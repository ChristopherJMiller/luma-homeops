default: boot

.PHONY: boot
boot:
	make -C metal boot

.PHONY: arch
arch:
	make -C metal install
