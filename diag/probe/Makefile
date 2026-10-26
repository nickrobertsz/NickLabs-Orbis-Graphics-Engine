# orbis-gfx-probe — NickLabs Orbis Graphics Pack. Needs only Vulkan headers (libraries are dlopen()ed).
# Jaguar-safe baseline; built with -march=btver2 for the PS4 by the package recipe.
CC        ?= cc
ARCHFLAGS ?= -march=x86-64 -mtune=btver2
CFLAGS    ?= -O2 -g -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers
VK_INCLUDE ?= /usr/include
PREFIX    ?= /usr
LIBEXEC   ?= $(PREFIX)/lib/orbis-graphics

orbis-gfx-probe: orbis-gfx-probe.c shaders_spv.h
	$(CC) $(ARCHFLAGS) $(CFLAGS) -std=gnu11 -I$(VK_INCLUDE) -o $@ $< -ldl

# Generates shaders_raster_spv.h (and refreshes shaders_spv.h). Needs glslc (shaderc).
shaders:
	python3 gen_spv_header.py

require-raster:
	@test -f shaders_raster_spv.h || { echo "shaders_raster_spv.h missing: run 'make shaders' (needs glslc)"; exit 1; }

install: orbis-gfx-probe
	install -Dm0755 orbis-gfx-probe $(DESTDIR)$(LIBEXEC)/orbis-gfx-probe

clean:
	rm -f orbis-gfx-probe shaders/*.spv

.PHONY: install clean shaders require-raster
