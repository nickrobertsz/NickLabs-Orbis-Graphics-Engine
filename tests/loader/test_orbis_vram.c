/* Host unit test for loader/orbis_vram.h (the header the Graphics Engine loader patch adds). */
#include <stdio.h>
#include <stdlib.h>
#include "orbis_vram.h"

static int fails;
#define CHECK(c, ...) do { if (!(c)) { fails++; printf("FAIL %s:%d ", __FILE__, __LINE__); printf(__VA_ARGS__); printf("\n"); } } while (0)

/* does MTRR (base, mask) match physical address a? (40-bit physical, 4 KiB granule) */
static int mtrr_hit(unsigned long long base, unsigned long long mask, unsigned long long a)
{
	unsigned long long m = mask & 0xfffffff000ULL;
	return (a & m) == (base & m);
}

int main(void)
{
	const unsigned long long G4 = 0x100000000ULL, MB = 1ULL << 20;
	int mb, valid = 0;

	for (mb = 0; mb <= 5000; mb++) {
		struct orbis_vram_block b[ORBIS_VRAM_MAX_BLOCKS];
		int n, i;
		unsigned long long total = 0;

		if (!orbis_vram_valid(mb))
			continue;
		valid++;
		CHECK(mb % 16 == 0 && mb >= 64 && mb <= 4096, "accepted %d", mb);
		n = orbis_vram_split(G4, (unsigned long long)mb * MB, b, ORBIS_VRAM_MAX_BLOCKS);
		CHECK(n >= 1 && n <= ORBIS_VRAM_MAX_BLOCKS, "split %d -> %d blocks", mb, n);
		for (i = 0; i < n; i++) {
			unsigned long long mask = orbis_mtrr_mask(b[i].size);
			CHECK((b[i].size & (b[i].size - 1)) == 0, "%d: block %d size not pow2", mb, i);
			CHECK((b[i].base & (b[i].size - 1)) == 0, "%d: block %d not aligned", mb, i);
			CHECK(i == 0 || b[i].base == b[i - 1].base + b[i - 1].size, "%d: gap", mb);
			/* the MTRR covers its block exactly: first/last byte in, neighbours out */
			CHECK(mtrr_hit(b[i].base, mask, b[i].base), "%d: start", mb);
			CHECK(mtrr_hit(b[i].base, mask, b[i].base + b[i].size - 1), "%d: end", mb);
			CHECK(!mtrr_hit(b[i].base, mask, b[i].base + b[i].size), "%d: overrun", mb);
			CHECK(b[i].base == 0 || !mtrr_hit(b[i].base, mask, b[i].base - 1), "%d: underrun", mb);
			total += b[i].size;
		}
		CHECK(total == (unsigned long long)mb * MB, "%d: total", mb);
		/* sample addresses: inside the carve-out => some MTRR hit; just after => none */
		{
			unsigned long long a;
			for (a = G4; a < G4 + (unsigned long long)mb * MB; a += 37 * MB) {
				int hit = 0;
				for (i = 0; i < n; i++)
					hit |= mtrr_hit(b[i].base, orbis_mtrr_mask(b[i].size), a);
				CHECK(hit, "%d: 0x%llx uncovered", mb, a);
			}
			a = G4 + (unsigned long long)mb * MB;
			for (i = 0; i < n; i++)
				CHECK(!mtrr_hit(b[i].base, orbis_mtrr_mask(b[i].size), a), "%d: covers RAM after", mb);
		}
	}
	/* the upstream single-MTRR mask is invalid for 3072 MiB (non-contiguous) */
	{
		unsigned long long up = (0xffffffffffULL - 3072ULL * MB + 1);
		int contiguous = 1, seen0 = 0, bit;
		for (bit = 39; bit >= 12; bit--) {
			int v = (up >> bit) & 1;
			if (!v) seen0 = 1; else if (seen0) contiguous = 0;
		}
		CHECK(!contiguous, "upstream 3072 mask unexpectedly contiguous");
	}
	CHECK(orbis_vram_valid(768) && orbis_vram_valid(3072) && orbis_vram_valid(256), "common sizes");
	CHECK(!orbis_vram_valid(1000) && !orbis_vram_valid(4609) && !orbis_vram_valid(32), "bad sizes");
	{
		struct orbis_vram_block b[2];
		CHECK(orbis_vram_split(G4, 7 * 256 * MB, b, 2) == -1, "too many blocks rejected");
		CHECK(orbis_vram_split(G4 + 256 * MB, 1024 * MB, b, 2) == -1, "misaligned rejected");
	}
	printf("%d valid sizes checked, %s\n", valid, fails ? "FAILED" : "OK");
	return fails ? 1 : 0;
}
