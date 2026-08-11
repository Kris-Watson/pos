# -*- coding: utf-8 -*-
from setuptools import setup, find_packages

# Requirements are listed in requirements.txt (frappe/erpnext come from the bench env).
# Filter blanks and comments so an effectively-empty file doesn't yield an invalid "" req.
with open("requirements.txt") as f:
    install_requires = [
        line.strip()
        for line in f.read().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

# get version from __version__ variable in pos/__init__.py
from pos import __version__ as version

setup(
    name="pos",
    version=version,
    description="Self-checkout point of sale (HitPay payments) on ERPNext",
    author="Self Checkout",
    author_email="",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires,
)
