/* Host test for orbis_cmdline_compose() extracted from the loader patch (main-aio.c hunk). */
#include <stdio.h>
#include <string.h>
#include "extracted_cmdline.inc"

int main(void)
{
	static char out[4096];
	int fails = 0;

	if (orbis_cmdline_compose(out, sizeof(out), "panic=0 console=tty0", 1024, 0) ||
	    strcmp(out, "panic=0 console=tty0 orbis.loader=ge2 orbis.vram_mb=1024")) {
		printf("FAIL no-gtt: '%s'\n", out); fails++;
	}
	if (orbis_cmdline_compose(out, sizeof(out), "a=b", 512, 4096) ||
	    strcmp(out, "a=b orbis.loader=ge2 orbis.vram_mb=512 orbis.gtt_mb=4096 ttm.pages_limit=1048576 amdgpu.gttsize=4096")) {
		printf("FAIL gtt: '%s'\n", out); fails++;
	}
	{
		char small[24];
		if (orbis_cmdline_compose(small, sizeof(small), "0123456789", 1024, 0) != -1) {
			printf("FAIL overflow not detected: '%s'\n", small); fails++;
		}
	}
	{
		static char big[5000];
		memset(big, 'x', sizeof(big) - 1);
		if (orbis_cmdline_compose(out, sizeof(out), big, 1024, 0) != -1) {
			printf("FAIL long source not rejected\n"); fails++;
		}
	}
	printf("cmdline %s\n", fails ? "FAILED" : "OK");
	return fails != 0;
}
