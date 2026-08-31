obj-m += raw_hw_rng.o

KDIR  ?= /lib/modules/$(shell uname -r)/build
PWD   := $(shell pwd)

.PHONY: all clean install uninstall load unload status

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean

# Load the freshly built module for this session only (needs root).
load:
	sudo insmod raw_hw_rng.ko
	@echo "--- dmesg ---"
	@dmesg | tail -3
	@ls -l /dev/raw_* 2>/dev/null || echo "No /dev/raw_* device was created."

unload:
	sudo rmmod raw_hw_rng

status:
	@lsmod | grep -q '^raw_hw_rng' \
		&& echo "module loaded, backend: $$(cat /sys/module/raw_hw_rng/parameters/active_backend)" \
		|| echo "module not loaded"
	@ls -l /dev/raw_* 2>/dev/null || true

# Install permanently so the module survives a reboot (needs root).
install: all
	sudo $(MAKE) -C $(KDIR) M=$(PWD) modules_install
	sudo depmod -a
	@echo raw_hw_rng | sudo tee /etc/modules-load.d/raw_hw_rng.conf > /dev/null
	@echo "Installed. The module will now load automatically at boot."

uninstall:
	-sudo rmmod raw_hw_rng
	sudo rm -f /etc/modules-load.d/raw_hw_rng.conf
	sudo rm -f /lib/modules/$(shell uname -r)/extra/raw_hw_rng.ko
	sudo depmod -a
	@echo "Uninstalled."
