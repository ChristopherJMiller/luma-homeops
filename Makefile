default: boot

.PHONY: boot
boot:
	make -C metal boot

.PHONY: arch
arch:
	make -C metal install

.PHONY: k3s
k3s:
	make -C metal k3s
