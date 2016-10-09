/* ref: https://github.com/bbu/userland-slab-allocator */

#include "slab.h"

#include <stdint.h>
#include <stddef.h>

#define assert(x) // turn off asserts

#define SLAB_DUMP_COLOURED

#ifdef SLAB_DUMP_COLOURED
# define GRAY(s)   "\033[1;30m" s "\033[0m"
# define RED(s)    "\033[0;31m" s "\033[0m"
# define GREEN(s)  "\033[0;32m" s "\033[0m"
# define YELLOW(s) "\033[1;33m" s "\033[0m"
#else
# define GRAY(s)   s
# define RED(s)    s
# define GREEN(s)  s
# define YELLOW(s) s
#endif

#define SLOTS_ALL_ZERO ((uint64_t) 0)
#define SLOTS_FIRST ((uint64_t) 1)
#define FIRST_FREE_SLOT(s) ((size_t) __builtin_ctzll(s))
#define FREE_SLOTS(s) ((size_t) __builtin_popcountll(s))
#define ONE_USED_SLOT(slots, empty_slotmask) \
    ( \
        ( \
            (~(slots) & (empty_slotmask))       & \
            ((~(slots) & (empty_slotmask)) - 1)   \
        ) == SLOTS_ALL_ZERO \
    )

#define POWEROF2(x) ((x) != 0 && ((x) & ((x) - 1)) == 0)

#define LIKELY(exp) __builtin_expect(exp, 1)
#define UNLIKELY(exp) __builtin_expect(exp, 0)

const size_t slab_pagesize = 0x1000;

static SceUID sce_exe_alloc(SceUID pid, void **ptr, uintptr_t *exe_addr, SceUID *exe_res, size_t align, size_t size) {
    return 0;
}

static int sce_exe_free(SceUID write_res, SceUID exe_res) {
    return 0;
}

static inline uint32_t next_pow_2(uint32_t v) {
    v--;
    v |= v >> 1;
    v |= v >> 2;
    v |= v >> 4;
    v |= v >> 8;
    v |= v >> 16;
    v++;
    v += (v == 0);
    return v;
}

void slab_init(struct slab_chain *const sch, const size_t itemsize, SceUID pid)
{
    assert(sch != NULL);
    assert(itemsize >= 1 && itemsize <= SIZE_MAX);
    assert(POWEROF2(slab_pagesize));

    sch->itemsize = itemsize;
    sch->pid = pid;

    const size_t data_offset = offsetof(struct slab_header, data);
    const size_t least_slabsize = data_offset + 64 * sch->itemsize;
    sch->slabsize = (size_t) next_pow_2(least_slabsize);
    sch->itemcount = 64;

    if (sch->slabsize - least_slabsize != 0) {
        const size_t shrinked_slabsize = sch->slabsize >> 1;

        if (data_offset < shrinked_slabsize &&
            shrinked_slabsize - data_offset >= 2 * sch->itemsize) {

            sch->slabsize = shrinked_slabsize;
            sch->itemcount = (shrinked_slabsize - data_offset) / sch->itemsize;
        }
    }

    sch->pages_per_alloc = sch->slabsize > slab_pagesize ?
        sch->slabsize : slab_pagesize;

    sch->empty_slotmask = ~SLOTS_ALL_ZERO >> (64 - sch->itemcount);
    sch->initial_slotmask = sch->empty_slotmask ^ SLOTS_FIRST;
    sch->alignment_mask = ~(sch->slabsize - 1);
    sch->partial = sch->empty = sch->full = NULL;

    assert(slab_is_valid(sch));
}

void *slab_alloc(struct slab_chain *const sch, uintptr_t *exe_addr)
{
    assert(sch != NULL);
    assert(slab_is_valid(sch));

    if (LIKELY(sch->partial != NULL)) {
        /* found a partial slab, locate the first free slot */
        register const size_t slot = FIRST_FREE_SLOT(sch->partial->slots);
        sch->partial->slots ^= SLOTS_FIRST << slot;

        if (UNLIKELY(sch->partial->slots == SLOTS_ALL_ZERO)) {
            /* slab has become full, change state from partial to full */
            struct slab_header *const tmp = sch->partial;

            /* skip first slab from partial list */
            if (LIKELY((sch->partial = sch->partial->next) != NULL))
                sch->partial->prev = NULL;

            if (LIKELY((tmp->next = sch->full) != NULL))
                sch->full->prev = tmp;

            sch->full = tmp;
            *exe_addr = sch->full->exe_data + slot * sch->itemsize;
            return sch->full->data + slot * sch->itemsize;
        } else {
            *exe_addr = sch->partial->exe_data + slot * sch->itemsize;
            return sch->partial->data + slot * sch->itemsize;
        }
    } else if (LIKELY((sch->partial = sch->empty) != NULL)) {
        /* found an empty slab, change state from empty to partial */
        if (LIKELY((sch->empty = sch->empty->next) != NULL))
            sch->empty->prev = NULL;

        sch->partial->next = NULL;

        /* slab is located either at the beginning of page, or beyond */
        UNLIKELY(sch->partial->refcount != 0) ?
            sch->partial->refcount++ : sch->partial->page->refcount++;

        sch->partial->slots = sch->initial_slotmask;
        *exe_addr = sch->partial->exe_data;
        return sch->partial->data;
    } else {
        /* no empty or partial slabs available, create a new one */
        SceUID write_res, exe_res;
        uintptr_t exe_data;
        if ((write_res = sce_exe_alloc(sch->pid, (void **)&sch->partial, &exe_data, 
                          &exe_res, sch->slabsize, sch->pages_per_alloc)) < 0) {
            *exe_addr = 0;
            return sch->partial = NULL;
        }
        sch->partial->write_res = write_res;
        sch->partial->exe_res = exe_res;
        sch->partial->exe_data = exe_data + offsetof(struct slab_header, data);
        exe_data += sch->slabsize;

        struct slab_header *prev = NULL;

        const char *const page_end =
            (char *) sch->partial + sch->pages_per_alloc;

        union {
            const char *c;
            struct slab_header *const s;
        } curr = {
            .c = (const char *) sch->partial + sch->slabsize
        };

        __builtin_prefetch(sch->partial, 1);

        sch->partial->prev = sch->partial->next = NULL;
        sch->partial->refcount = 1;
        sch->partial->slots = sch->initial_slotmask;

        if (LIKELY(curr.c != page_end)) {
            curr.s->prev = NULL;
            curr.s->refcount = 0;
            curr.s->page = sch->partial;
            curr.s->write_res = write_res;
            curr.s->exe_res = exe_res;
            curr.s->exe_data = exe_data + offsetof(struct slab_header, data);
            exe_data += sch->slabsize;
            curr.s->slots = sch->empty_slotmask;
            sch->empty = prev = curr.s;

            while (LIKELY((curr.c += sch->slabsize) != page_end)) {
                prev->next = curr.s;
                curr.s->prev = prev;
                curr.s->refcount = 0;
                curr.s->page = sch->partial;
                curr.s->write_res = write_res;
                curr.s->exe_res = exe_res;
                curr.s->exe_data = exe_data + offsetof(struct slab_header, data);
                exe_data += sch->slabsize;
                curr.s->slots = sch->empty_slotmask;
                prev = curr.s;
            }

            prev->next = NULL;
        }

        *exe_addr = sch->partial->exe_data;
        return sch->partial->data;
    }

    /* unreachable */
}

void slab_free(struct slab_chain *const sch, const void *const addr)
{
    assert(sch != NULL);
    assert(slab_is_valid(sch));
    assert(addr != NULL);

    struct slab_header *const slab = (void *)
        ((uintptr_t) addr & sch->alignment_mask);

    register const int slot = ((char *) addr - (char *) slab -
        offsetof(struct slab_header, data)) / sch->itemsize;

    if (UNLIKELY(slab->slots == SLOTS_ALL_ZERO)) {
        /* target slab is full, change state to partial */
        slab->slots = SLOTS_FIRST << slot;

        if (LIKELY(slab != sch->full)) {
            if (LIKELY((slab->prev->next = slab->next) != NULL))
                slab->next->prev = slab->prev;

            slab->prev = NULL;
        } else if (LIKELY((sch->full = sch->full->next) != NULL)) {
            sch->full->prev = NULL;
        }

        slab->next = sch->partial;

        if (LIKELY(sch->partial != NULL))
            sch->partial->prev = slab;

        sch->partial = slab;
    } else if (UNLIKELY(ONE_USED_SLOT(slab->slots, sch->empty_slotmask))) {
        /* target slab is partial and has only one filled slot */
        if (UNLIKELY(slab->refcount == 1 || (slab->refcount == 0 &&
            slab->page->refcount == 1))) {

            /* unmap the whole page if this slab is the only partial one */
            if (LIKELY(slab != sch->partial)) {
                if (LIKELY((slab->prev->next = slab->next) != NULL))
                    slab->next->prev = slab->prev;
            } else if (LIKELY((sch->partial = sch->partial->next) != NULL)) {
                sch->partial->prev = NULL;
            }

            void *const page = UNLIKELY(slab->refcount != 0) ? slab : slab->page;
            const char *const page_end = (char *) page + sch->pages_per_alloc;
            char found_head = 0;

            union {
                const char *c;
                const struct slab_header *const s;
            } s;

            for (s.c = page; s.c != page_end; s.c += sch->slabsize) {
                if (UNLIKELY(s.s == sch->empty))
                    found_head = 1;
                else if (UNLIKELY(s.s == slab))
                    continue;
                else if (LIKELY((s.s->prev->next = s.s->next) != NULL))
                    s.s->next->prev = s.s->prev;
            }

            if (UNLIKELY(found_head && (sch->empty = sch->empty->next) != NULL))
                sch->empty->prev = NULL;

            sce_exe_free(slab->write_res, slab->exe_res);
        } else {
            slab->slots = sch->empty_slotmask;

            if (LIKELY(slab != sch->partial)) {
                if (LIKELY((slab->prev->next = slab->next) != NULL))
                    slab->next->prev = slab->prev;

                slab->prev = NULL;
            } else if (LIKELY((sch->partial = sch->partial->next) != NULL)) {
                sch->partial->prev = NULL;
            }

            slab->next = sch->empty;

            if (LIKELY(sch->empty != NULL))
                sch->empty->prev = slab;

            sch->empty = slab;

            UNLIKELY(slab->refcount != 0) ?
                slab->refcount-- : slab->page->refcount--;
        }
    } else {
        /* target slab is partial, no need to change state */
        slab->slots |= SLOTS_FIRST << slot;
    }
}

uintptr_t slab_getmirror(struct slab_chain *const sch, const void *const addr)
{
    assert(sch != NULL);
    assert(slab_is_valid(sch));
    assert(addr != NULL);

    struct slab_header *const slab = (void *)
        ((uintptr_t) addr & sch->alignment_mask);


    return slab->exe_data + (ptrdiff_t)((char *) addr - (char *) slab);
}

void slab_traverse(const struct slab_chain *const sch, void (*fn)(const void *))
{
    assert(sch != NULL);
    assert(fn != NULL);
    assert(slab_is_valid(sch));

    const struct slab_header *slab;
    const char *item, *end;
    const size_t data_offset = offsetof(struct slab_header, data);

    for (slab = sch->partial; slab; slab = slab->next) {
        item = (const char *) slab + data_offset;
        end = item + sch->itemcount * sch->itemsize;
        uint64_t mask = SLOTS_FIRST;

        do {
            if (!(slab->slots & mask))
                fn(item);

            mask <<= 1;
        } while ((item += sch->itemsize) != end);
    }

    for (slab = sch->full; slab; slab = slab->next) {
        item = (const char *) slab + data_offset;
        end = item + sch->itemcount * sch->itemsize;

        do fn(item);
        while ((item += sch->itemsize) != end);
    }
}

void slab_destroy(const struct slab_chain *const sch)
{
    assert(sch != NULL);
    assert(slab_is_valid(sch));

    struct slab_header *const heads[] = {sch->partial, sch->empty, sch->full};
    struct slab_header *pages_head = NULL, *pages_tail;

    for (size_t i = 0; i < 3; ++i) {
        struct slab_header *slab = heads[i];

        while (slab != NULL) {
            if (slab->refcount != 0) {
                struct slab_header *const page = slab;
                slab = slab->next;

                if (UNLIKELY(pages_head == NULL))
                    pages_head = page;
                else
                    pages_tail->next = page;

                pages_tail = page;
            } else {
                slab = slab->next;
            }
        }
    }

    if (LIKELY(pages_head != NULL)) {
        pages_tail->next = NULL;
        struct slab_header *page = pages_head;

        do {
            sce_exe_free(page->write_res, page->exe_res);
            page = page->next;
        } while (page != NULL);
    }
}
