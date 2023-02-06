from setuptools import setup, find_packages

with open("requirements.txt") as f:
	install_requires = f.read().strip().split("\n")

# get version from __version__ variable in tap_lms/__init__.py
from tap_lms import __version__ as version

setup(
	name="tap_lms",
	version=version,
	description="Lms system for tap",
	author="Techt4dev",
	author_email="tech4dev@gmail.com",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)
