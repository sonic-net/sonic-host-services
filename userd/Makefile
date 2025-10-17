# Makefile for sonic-host-userd

.PHONY: all clean install build-cpp

# Default target
all: build-cpp

# Build C++ components
build-cpp:
	mkdir -p build
	cd build && cmake ../src && make

# Clean build artifacts
clean:
	rm -rf build

# Install (called by debian packaging)
install: build-cpp
	mkdir -p $(DESTDIR)/usr/local/bin
	cp build/userd $(DESTDIR)/usr/local/bin/userd
	chmod +x $(DESTDIR)/usr/local/bin/userd

# For development/testing
install-local: build-cpp
	sudo cp build/userd /usr/local/bin/userd
	sudo chmod +x /usr/local/bin/userd
