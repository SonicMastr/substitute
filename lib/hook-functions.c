#include "substitute-internal.h"
#ifdef TARGET_DIS_SUPPORTED
#include "substitute.h"
#include "jump-dis.h"
#include "transform-dis.h"
#include "execmem.h"
#include "arm/jump-patch.h"
#include <stdlib.h>
#include <alloca.h>
#ifndef NO_PTHREADS
#include <pthread.h>
#endif

struct hook_internal
{
	uint8_t jump_patch[MAX_JUMP_PATCH_SIZE];
	size_t jump_patch_size;
	void *code;
	void *outro_trampoline;
	/* page allocated with execmem_alloc_unsealed - only if we had to allocate
	 * one when processing this hook */
	void *trampoline_page;
	struct arch_dis_ctx arch_dis_ctx;
};

EXPORT
int substitute_hook_functions(const struct substitute_function_hook *hooks,
							  size_t nhooks,
							  struct substitute_function_hook_record **recordp,
							  int options) {
#ifndef NO_PTHREADS
	bool thread_safe = !(options & SUBSTITUTE_NO_THREAD_SAFETY);
	if (thread_safe && !pthread_main_np())
		return SUBSTITUTE_ERR_NOT_ON_MAIN_THREAD;
#else
	bool thread_safe = false;
#endif
	bool relaxed = !!(options & SUBSTITUTE_RELAXED);

	if (recordp)
		*recordp = NULL;

	struct execmem_foreign_write *fws;
	struct hook_internal *his = alloca(nhooks * sizeof(*his) +
									   nhooks * sizeof(*fws));
	if (!his)
		return SUBSTITUTE_ERR_OOM;
	fws = (void *)(his + nhooks);

	for (size_t i = 0; i < nhooks; i++)
		his[i].trampoline_page = NULL;

	int ret = SUBSTITUTE_OK;

	void *trampoline_prev = NULL;
	void *trampoline_ptr = NULL;
	uintptr_t trampoline_addr = 0;
	size_t trampoline_size_left = 0;

	/* First run through and (a) ensure all the functions are OK to hook, (b)
	 * allocate memory for the trampolines. */
	for (size_t i = 0; i < nhooks; i++) {
		const struct substitute_function_hook *hook = &hooks[i];
		struct hook_internal *hi = &his[i];
		void *code = hook->function;
		struct arch_dis_ctx arch;
		arch_dis_ctx_init(&arch);
#ifdef __arm__
		if ((uintptr_t)code & 1) {
			arch.pc_low_bit = true;
			code--;
		}
#endif
		hi->code = code;
		hi->arch_dis_ctx = arch;
		uintptr_t pc_patch_start = (uintptr_t)code;
		int patch_size = jump_patch_size(pc_patch_start, (uintptr_t)hook->replacement, arch, false);
		uint_tptr pc_patch_end = pc_patch_start + patch_size;
		uintptr_t initial_target = (uintptr_t)hook->replacement;

		/* Make the real jump patch for the target function. */
		void *jp = hi->jump_patch;
		make_jump_patch(&jp, pc_patch_start, initial_target, arch);
		hi->jump_patch_size = (uint8_t *)jp - hi->jump_patch;

		size_t outro_est = TD_MAX_REWRITTEN_SIZE + MAX_JUMP_PATCH_SIZE;

		if (outro_est > trampoline_size_left) {
			/* Not enough space left in our existing block... */
			if ((ret = execmem_alloc_unsealed(0, &trampoline_ptr,
											  &trampoline_addr,
											  &trampoline_size_left,
											  hook->opt)))
				goto end;
			/* NOTE: We assume that each page is large enough (min
			 * TD_MAX_REWRITTEN_SIZE + 2 * MAX_JUMP_PATCH_SIZE) so we don't lose
			 * a reference by having one hook allocate two pages. Also must
			 * ensure this size is aligned to ARCH_MAX_CODE_ALIGNMENT otherwise
			 * MAX_JUMP_PATCH_SIZE might be wrong. */
			hi->trampoline_page = trampoline_ptr;
		}

		void *outro_trampoline_real = trampoline_ptr;
		hi->outro_trampoline = outro_trampoline_real;
#ifdef __arm__
		if (arch.pc_low_bit)
			hi->outro_trampoline++;
#endif
		if (hook->old_ptr)
			*(uintptr_t *)hook->old_ptr = trampoline_addr +
										  (uintptr_t)(hi->outro_trampoline - outro_trampoline_real);

		/* Generate the rewritten start of the function for the outro
		 * trampoline (complaining if any bad instructions are found)
		 * (on arm64, this modifies arch.regs_possibly_written, which is used
		 * by the later make_jump_patch call) */
		trampoline_prev = trampoline_ptr;
		if ((ret = transform_dis_main(code, &trampoline_ptr, pc_patch_start,
									  &pc_patch_end, trampoline_addr,
									  &arch,
									  (thread_safe ? TRANSFORM_DIS_BAN_CALLS : 0) |
										  (relaxed ? 0 : TRANSFORM_DIS_REL_JUMPS))))
			goto end;
		trampoline_addr += (trampoline_ptr - trampoline_prev);

		uintptr_t dpc = pc_patch_end;
#ifdef __arm__
		if (arch.pc_low_bit)
			dpc++;
#endif

		/* Now that transform_dis_main has given us the final pc_patch_end,
		 * check some of the rest of the function for jumps back into the
		 * patched region. */
		if ((ret = jump_dis_main(code, pc_patch_start, pc_patch_end, arch)))
			goto end;
		/* Okay, continue with the outro. */
		trampoline_prev = trampoline_ptr;
		make_jump_patch(&trampoline_ptr, trampoline_addr, dpc, arch);
		trampoline_addr += (trampoline_ptr - trampoline_prev);

		trampoline_ptr += -(uintptr_t)trampoline_ptr % ARCH_MAX_CODE_ALIGNMENT;
		trampoline_addr += -trampoline_addr % ARCH_MAX_CODE_ALIGNMENT;
		trampoline_size_left -= (uint8_t *)trampoline_ptr - (uint8_t *)outro_trampoline_real;
	}

	/* room to save records */
	struct substitute_function_hook_record *records = NULL;

	if (recordp) {
		records = malloc(nhooks * (sizeof(struct substitute_function_hook_record) +
								   MAX_JUMP_PATCH_SIZE));
		*recordp = records;
	}

	/* Now commit. */
	for (size_t i = 0; i < nhooks; i++) {
		struct hook_internal *hi = &his[i];
		void *page = hi->trampoline_page;
		if (page)
			execmem_seal(page, hooks[i].opt);
		fws[i].dst = hi->code;
		fws[i].src = hi->jump_patch;
		fws[i].len = hi->jump_patch_size;
		fws[i].opt = hooks[i].opt;
		if (records) {
			records->function = hi->code;
			records->opt = hooks[i].opt;
			records->buffer_size = hi->jump_patch_size;
			memcpy(records->saved_buffer, hi->code, hi->jump_patch_size);
			records = (struct substitute_function_hook_record *)((char *)&records->saved_buffer + records->buffer_size);
		}
	}

	ret = execmem_foreign_write_with_pc_patch(fws, nhooks);
	if (ret) {
		/* Too late to free the trampolines.  Chances are this is fatal anyway. */
		goto end_dont_free;
	}

	goto end_dont_free;
end:
	/* if we failed, get rid of the trampolines. */
	for (size_t i = 0; i < nhooks; i++) {
		void *page = his[i].trampoline_page;
		if (page)
			execmem_free(page, hooks[i].opt);
	}
	/* free records */
	if (recordp && *recordp)
		free(*recordp);
end_dont_free:
	return ret;
}

EXPORT
int substitute_free_hooks(struct substitute_function_hook_record *records,
						  size_t nhooks) {
	int ret;
	struct substitute_function_hook_record *cur = records;
	struct execmem_foreign_write *fws = alloca(nhooks * sizeof(*fws));
	for (int i = 0; i < nhooks; i++) {
		fws[i].dst = cur->function;
		fws[i].src = cur->saved_buffer;
		fws[i].len = cur->buffer_size;
		fws[i].opt = cur->opt;
		cur = (struct substitute_function_hook_record *)((char *)&cur->saved_buffer + cur->buffer_size);
	}
	/* TODO: Fix the case when thread is inside a patch/trampoline. */
	ret = execmem_foreign_write_with_pc_patch(fws, nhooks);
	free(records);
	return ret;
}

#endif /* TARGET_DIS_SUPPORTED */
