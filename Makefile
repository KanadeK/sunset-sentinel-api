PYTHON ?= python

.PHONY: verify demo benchmark package release-check

verify:
	$(PYTHON) scripts/verify.py

demo:
	$(PYTHON) scripts/demo.py
	$(PYTHON) scripts/generate_demo_image.py

benchmark:
	$(PYTHON) scripts/benchmark.py

package:
	$(PYTHON) scripts/package_release.py

release-check: package
	$(PYTHON) scripts/release_check.py
