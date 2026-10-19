.PHONY: test deb deb-private clean

test:
	python3 -m unittest discover -v

deb:
	python3 scripts/build_deb.py

deb-private:
	python3 scripts/build_deb.py --private

clean:
	rm -rf build dist
