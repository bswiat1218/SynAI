CC ?= cc
PYTHON ?= /usr/bin/python3
CPPFLAGS += $(shell pkg-config --cflags sqlite3)
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Werror -fPIC
LDFLAGS += -shared -Wl,-z,defs

.PHONY: all test clean
all: dirfd_vfs.so test_probe.so

%.so: %.c
	$(CC) $(CPPFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ $<

test: all
	$(PYTHON) -m unittest -v test_dirfd_vfs.py

clean:
	rm -f dirfd_vfs.so test_probe.so
	rm -rf __pycache__
