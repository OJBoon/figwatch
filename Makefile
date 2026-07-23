.PHONY: help install build clean

# Default target — show available commands
help:
	@echo ""
	@echo "FigWatch — available commands:"
	@echo ""
	@echo "  make install      Install build dependencies (run once before building)"
	@echo "  make build        Build the macOS app  →  macos/dist/FigWatch.app"
	@echo "  make clean        Remove build artefacts"
	@echo ""

# Install Python build dependencies needed to build the macOS app
install:
	python3.11 -m pip install setuptools py2app pyobjc
	# The .app bundles `anthropic` (gateway users audit via the Messages API,
	# no CLI), so its runtime deps must be importable for py2app to graph them.
	# pip install -e . pulls them from pyproject's [project].dependencies.
	python3.11 -m pip install -e .

# Build the macOS .app bundle
build:
	cd macos && python3.11 setup.py py2app

# Remove build artefacts (does not touch source files)
clean:
	rm -rf macos/build macos/dist
