#
# Copyright (c) nexB Inc. and others. All rights reserved.
# ScanCode is a trademark of nexB Inc.
# SPDX-License-Identifier: Apache-2.0
# See http://www.apache.org/licenses/LICENSE-2.0 for the license text.
# See https://github.com/nexB/scancode-toolkit for support or download.
# See https://aboutcode.org for more information about nexB OSS projects.
#

import ast
import base64
import io
import json
import logging
import os
import re
import sys
import zipfile
from configparser import ConfigParser
from pathlib import Path

import dparse2
import importlib_metadata
import pip_requirements_parser
import pkginfo2
from commoncode import fileutils
from packageurl import PackageURL
from packaging import markers
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from _packagedcode import models
from _packagedcode.utils import build_description

# FIXME: we always want to use the external library rather than the built-in for now
try:
    from zipfile import Path as ZipPath
except ImportError:
    from zipp import Path as ZipPath

"""
Detect and collect Python packages information.

Originally vendored from scancode-toolkit packagedcode.pypi
"""
# TODO: add support for poetry and setup.cfg and metadata.json
# TODO: add support for pex, pyz, etc.

TRACE = False


def logger_debug(*args):
    pass


logger = logging.getLogger(__name__)

if TRACE:
    logging.basicConfig(stream=sys.stdout)
    logger.setLevel(logging.DEBUG)

    def logger_debug(*args):
        return logger.debug(" ".join(isinstance(a, str) and a or repr(a) for a in args))


class PythonSdistPkgInfoFile(models.DatafileHandler):
    datasource_id = "pypi_sdist_pkginfo"
    default_package_type = "pypi"
    default_primary_language = "Python"
    path_patterns = ("*/PKG-INFO",)
    description = "PyPI extracted sdist PKG-INFO"
    documentation_url = "https://peps.python.org/pep-0314/"

    @classmethod
    def parse(cls, location):
        yield parse_metadata(
            location=location,
            datasource_id=cls.datasource_id,
            package_type=cls.default_package_type,
        )


class PythonInstalledWheelMetadataFile(models.DatafileHandler):
    datasource_id = "pypi_wheel_metadata"
    path_patterns = ("*.dist-info/METADATA",)
    default_package_type = "pypi"
    default_primary_language = "Python"
    description = "PyPI installed wheel METADATA"
    documentation_url = "https://packaging.python.org/en/latest/specifications/core-metadata/"

    @classmethod
    def parse(cls, location):
        yield parse_metadata(
            location=location,
            datasource_id=cls.datasource_id,
            package_type=cls.default_package_type,
        )


# FIXME: Implement me
class PyprojectTomlHandler(models.DatafileHandler):
    datasource_id = "pypi_pyproject_toml"
    path_patterns = ("*pyproject.toml",)
    default_package_type = "pypi"
    default_primary_language = "Python"
    description = "Python pyproject.toml"
    documentation_url = "https://peps.python.org/pep-0621/"


META_DIR_SUFFIXES = (
    ".dist-info",
    ".egg-info",
    "EGG-INFO",
)


def parse_metadata(location, datasource_id, package_type):
    """
    Return a PackageData object from a PKG-INFO or METADATA file at ``location``
    which is a path string or pathlib.Path-like object (including a possible zip
    file ZipPath for a wheel)

    Looks in neighboring files as needed when an installed layout is found.
    """
    path = location
    if not isinstance(location, (Path, ZipPath)):
        path = Path(location)

    # build from dir if we are an installed distro
    parent = path.parent
    if parent.name.endswith(META_DIR_SUFFIXES):
        path = parent

    dist = importlib_metadata.PathDistribution(path)

    meta = dist.metadata

    name = get_attribute(meta, "Name")
    version = get_attribute(meta, "Version")

    urls = get_urls(metainfo=meta, name=name, version=version)

    dependencies = get_dist_dependencies(dist)

    package_data = models.PackageData(
        datasource_id=datasource_id,
        type=package_type,
        primary_language="Python",
        name=name,
        version=version,
        description=get_description(meta, location),
        declared_license=get_declared_license(meta),
        keywords=get_keywords(meta),
        parties=get_parties(meta),
        dependencies=dependencies,
        **urls,
    )

    return package_data


def urlsafe_b64decode(data):
    """
    urlsafe_b64decode without padding
    SPDX-License-Identifier: MIT
    Copyright (c) 2012-2014 Daniel Holth <dholth@fastmail.fm> and contributors.
    From: https://github.com/pypa/wheel/blob/66208910ab51f4008b034ef4833acfdc920f7606/src/wheel/util.py#L23
    """
    pad = b"=" * (4 - (len(data) & 3))
    return base64.urlsafe_b64decode(data.encode("ASCII") + pad)


class PypiWheelHandler(models.DatafileHandler):
    datasource_id = "pypi_wheel"
    path_patterns = ("*.whl",)
    # filetypes = ('zip archive',)
    default_package_type = "pypi"
    default_primary_language = "Python"
    description = "PyPI wheel"
    documentation_url = "https://peps.python.org/pep-0427/"

    @classmethod
    def parse(cls, location):
        with zipfile.ZipFile(location) as zf:
            for path in ZipPath(zf).iterdir():
                if not path.name.endswith(META_DIR_SUFFIXES):
                    continue
                for metapath in path.iterdir():
                    if not metapath.name.endswith("METADATA"):
                        continue

                    yield parse_metadata(
                        location=metapath,
                        datasource_id=cls.datasource_id,
                        package_type=cls.default_package_type,
                    )


class PypiEggHandler(models.DatafileHandler):
    datasource_id = "pypi_egg"
    path_patterns = ("*.egg",)
    # filetypes = ('zip archive',)
    default_package_type = "pypi"
    default_primary_language = "Python"
    description = "PyPI egg"
    documentation_url = "https://web.archive.org/web/20210604075235/http://peak.telecommunity.com/DevCenter/PythonEggs"

    @classmethod
    def parse(cls, location):
        with zipfile.ZipFile(location) as zf:
            for path in ZipPath(zf).iterdir():
                if not path.name.endswith(META_DIR_SUFFIXES):
                    continue

                for metapath in path.iterdir():
                    if not metapath.name.endswith("PKG-INFO"):
                        continue

                    yield parse_metadata(
                        location=metapath,
                        datasource_id=cls.datasource_id,
                        package_type=cls.default_package_type,
                    )


class PypiSdistArchiveHandler(models.DatafileHandler):
    datasource_id = "pypi_sdist"
    path_patterns = (
        "*.tar.gz",
        "*.tar.bz2",
        "*.zip",
    )
    default_package_type = "pypi"
    default_primary_language = "Python"
    description = "Python source distribution"
    documentation_url = "https://peps.python.org/pep-0643/"

    @classmethod
    def is_datafile(cls, location, filetypes=tuple()):
        if super().is_datafile(location, filetypes=filetypes):
            # TODO: there is a structure to an sdists name: aboutcode-toolkit-7.0.0.tar.gz
            # TODO: there is more to it than this... based on actual listing of files inside
            return True

    @classmethod
    def parse(cls, location):
        # FIXME: add dependencies

        try:
            sdist = pkginfo2.SDist(location)
        except ValueError:
            return

        name = sdist.name
        version = sdist.version
        urls = get_urls(metainfo=sdist, name=name, version=version)

        yield models.PackageData(
            datasource_id=cls.datasource_id,
            type=cls.default_package_type,
            primary_language=cls.default_primary_language,
            name=name,
            version=version,
            description=get_description(sdist, location=location),
            declared_license=get_declared_license(sdist),
            keywords=get_keywords(sdist),
            parties=get_parties(sdist),
            **urls,
        )


class PythonSetupPyHandler(models.DatafileHandler):
    datasource_id = "pypi_setup_py"
    path_patterns = ("*setup.py",)
    default_package_type = "pypi"
    default_primary_language = "Python"
    description = "Python setup.py"
    documentation_url = "https://docs.python.org/3/distutils/setupscript.html"

    @classmethod
    def parse(cls, location):
        setup_args = get_setup_py_args(location)

        # it may be legit to have a name-less package?
        # in anycase we do not want to fail because of that
        name = setup_args.get("name")

        version = setup_args.get("version")
        if not version:
            # search for possible dunder versions here and elsewhere
            version = detect_version_attribute(location)

        urls = get_urls(metainfo=setup_args, name=name, version=version)
        yield models.PackageData(
            datasource_id=cls.datasource_id,
            type=cls.default_package_type,
            primary_language=cls.default_primary_language,
            name=name,
            version=version,
            description=get_description(setup_args),
            parties=get_parties(setup_args),
            declared_license=get_declared_license(setup_args),
            dependencies=get_setup_py_dependencies(setup_args),
            keywords=get_keywords(setup_args),
            **urls,
        )


class BaseDependencyFileHandler(models.DatafileHandler):
    """
    Base class for a dependency files parsed with the same library
    """

    @classmethod
    def parse(cls, location):
        file_name = fileutils.file_name(location)

        dependency_type = get_dparse2_supported_file_name(file_name)
        if not dependency_type:
            return

        dependencies = parse_with_dparse2(
            location=location,
            file_name=dependency_type,
        )
        yield models.PackageData(
            datasource_id=cls.datasource_id,
            type=cls.default_package_type,
            primary_language=cls.default_primary_language,
            dependencies=dependencies,
        )


class SetupCfgHandler(models.DatafileHandler):
    datasource_id = "pypi_setup_cfg"
    path_patterns = ("*setup.cfg",)
    default_package_type = "pypi"
    default_primary_language = "Python"
    description = "Python setup.cfg"
    documentation_url = "https://peps.python.org/pep-0390/"

    @classmethod
    def parse(cls, location):
        file_name = fileutils.file_name(location)

        metadata = {}
        parser = ConfigParser()
        with open(location) as f:
            parser.read_file(f)

        for section in parser.values():
            if section.name == "metadata":
                options = (
                    "name",
                    "version",
                    "license",
                    "url",
                    "author",
                    "author_email",
                )
                for name in options:
                    content = section.get(name)
                    if not content:
                        continue
                    metadata[name] = content

        parties = []
        author = metadata.get("author")
        if author:
            parties = [
                models.Party(
                    type=models.party_person,
                    name=author,
                    role="author",
                    email=metadata.get("author_email"),
                )
            ]

        dependency_type = get_dparse2_supported_file_name(file_name)
        if not dependency_type:
            return

        dependencies = parse_with_dparse2(
            location=location,
            file_name=dependency_type,
        )
        yield models.PackageData(
            datasource_id=cls.datasource_id,
            type=cls.default_package_type,
            name=metadata.get("name"),
            version=metadata.get("version"),
            parties=parties,
            homepage_url=metadata.get("url"),
            primary_language=cls.default_primary_language,
            dependencies=dependencies,
        )


class PipfileHandler(BaseDependencyFileHandler):
    datasource_id = "pipfile"
    path_patterns = ("*Pipfile",)
    default_package_type = "pypi"
    default_primary_language = "Python"
    description = "Pipfile"
    documentation_url = "https://github.com/pypa/pipfile"


class PipfileLockHandler(BaseDependencyFileHandler):
    datasource_id = "pipfile_lock"
    path_patterns = ("*Pipfile.lock",)
    default_package_type = "pypi"
    default_primary_language = "Python"
    description = "Pipfile.lock"
    documentation_url = "https://github.com/pypa/pipfile"

    @classmethod
    def parse(cls, location):
        with open(location) as f:
            content = f.read()

        data = json.loads(content)

        sha256 = None
        if "_meta" in data:
            for name, meta in data["_meta"].items():
                if name == "hash":
                    sha256 = meta.get("sha256")

        dependent_packages = parse_with_dparse2(
            location=location,
            file_name="Pipfile.lock",
        )

        yield models.PackageData(
            datasource_id=cls.datasource_id,
            type=cls.default_package_type,
            primary_language=cls.default_primary_language,
            sha256=sha256,
            dependencies=dependent_packages,
        )


class PipRequirementsFileHandler(BaseDependencyFileHandler):
    datasource_id = "pip_requirements"

    path_patterns = (
        "*requirement*.txt",
        "*requirement*.pip",
        "*requirement*.in",
        "*requires.txt",
        "*requirements/*.txt",
        "*requirements/*.pip",
        "*requirements/*.in",
        "*reqs.txt",
    )

    default_package_type = "pypi"
    default_primary_language = "Python"
    description = "pip requirements file"
    documentation_url = "https://pip.pypa.io/en/latest/reference/requirements-file-format/"

    @classmethod
    def parse(cls, location):
        dependencies = get_requirements_txt_dependencies(location=location)
        yield models.PackageData(
            datasource_id=cls.datasource_id,
            type=cls.default_package_type,
            primary_language=cls.default_primary_language,
            dependencies=dependencies,
        )


# TODO: enable nested load


def get_requirements_txt_dependencies(location, include_nested=False):
    """
    Return a list of DependentPackage found in a requirements file at
    ``location`` or an empty list.
    """
    req_file = pip_requirements_parser.RequirementsFile.from_file(
        filename=location,
        include_nested=include_nested,
    )
    if not req_file or not req_file.requirements:
        return []

    dependent_packages = []

    # for now we ignore plain options and errors
    for req in req_file.requirements:

        if req.name:
            # will be None if not pinned
            version = req.get_pinned_version
            purl = PackageURL(type="pypi", name=req.name, version=version)

        else:
            # this is odd, but this can be null
            purl = None

        purl = purl and purl.to_string() or None

        if req.is_editable:
            requirement = req.dumps(with_name=False)
        else:
            requirement = req.dumps()

        if location.endswith(
            (
                "dev.txt",
                "test.txt",
                "tests.txt",
            )
        ):
            scope = "development"
            is_runtime = False
            is_optional = True
        else:
            scope = "install"
            is_runtime = True
            is_optional = False

        dependent_packages.append(
            models.DependentPackage(
                purl=purl,
                scope=scope,
                is_runtime=is_runtime,
                is_optional=is_optional,
                is_resolved=req.is_pinned or False,
                extracted_requirement=requirement,
            )
        )

    return dependent_packages


def get_attribute(metainfo, name, multiple=False):
    """
    Return the value for the attribute ``name`` in the ``metainfo`` mapping,
    pkginfo object or email object. Treat the value as a list of multiple values
    if ``multiple`` is True. Return None or an empty list (if multiple is True)
    if no value is found or the attribute ``name`` does not exist.
    Ignore case (but returns the value for the original case if present.
    """

    # note: the approach for this function is to be used with the various
    # metainfo objects and dictionsaries we use that can be a
    # pkginfo.Distribution, an email.message.EmailMessage or a dict.

    # Because of that, the key can be obtained as a plain named attribute,
    # either as-is or lowercased (and with dash replaced by dunder) or we
    # can use a get on dicts of emails.

    def attr_getter(_aname, default):
        _aname = _aname.replace("-", "_")
        return getattr(metainfo, _aname, default) or getattr(metainfo, _aname.lower(), default)

    def item_getter(_iname, getter, default):
        getter = getattr(metainfo, getter, None)
        if getter:
            return getter(_iname, default) or getter(_iname.lower(), default)
        return default

    if multiple:
        return (
            attr_getter(name, [])
            or item_getter(name, "get_all", [])
            or item_getter(name, "get", [])
            or []
        )
    else:
        return attr_getter(name, None) or item_getter(name, "get", None) or None


def get_description(metainfo, location=None):
    """
    Return a list of keywords found in a ``metainfo`` object or mapping.
    """
    description = None
    # newer metadata versions use the payload for the description
    if hasattr(metainfo, "get_payload"):
        description = metainfo.get_payload()
    description = description and description.strip() or None
    if not description:
        # legacymetadata versions use the Description for the description
        description = get_attribute(metainfo, "Description")
        if not description and location:
            # older metadata versions can use a DESCRIPTION.rst file
            description = get_legacy_description(location=fileutils.parent_directory(location))

    summary = get_attribute(metainfo, "Summary")
    description = clean_description(description)
    return build_description(summary, description)


def clean_description(description):
    """
    Return a cleaned description text, removing extra leading whitespaces if
    needed. Some metadata formats padd each description line with 8 spaces. Some
    do not. We check first and cleanup if needed.
    """
    # TODO: verify what is the impact of Description-Content-Type: if any
    description = description or ""
    description = description.strip()
    lines = description.splitlines(False)

    space_padding = " " * 8

    # we need cleaning if any of the first two lines starts with 8 spaces
    need_cleaning = any(l.startswith(space_padding) for l in lines[:2])
    if not need_cleaning:
        return description

    cleaned_lines = [line[8:] if line.startswith(space_padding) else line for line in lines]

    return "\n".join(cleaned_lines)


def get_legacy_description(location):
    """
    Return the text of a legacy DESCRIPTION.rst file.
    """
    location = os.path.join(location, "DESCRIPTION.rst")
    if os.path.exists(location):
        with open(location) as i:
            return i.read()


def get_declared_license(metainfo):
    """
    Return a mapping of declared license information found in a ``metainfo``
    object or mapping.
    """
    declared_license = {}
    # TODO: We should make the declared license as it is, this should be
    # updated in scancode to parse a pure string
    lic = get_attribute(metainfo, "License")
    if lic and not lic == "UNKNOWN":
        declared_license["license"] = lic

    license_classifiers, _ = get_classifiers(metainfo)
    if license_classifiers:
        declared_license["classifiers"] = license_classifiers
    return declared_license


def get_classifiers(metainfo):
    """
    Return a two tuple of lists of (license_classifiers, other_classifiers)
    found in a ``metainfo`` object or mapping.
    """

    classifiers = get_attribute(metainfo, "Classifier", multiple=True) or get_attribute(
        metainfo, "Classifiers", multiple=True
    )
    if not classifiers:
        return [], []

    license_classifiers = []
    other_classifiers = []
    for classifier in classifiers:
        if classifier.startswith("License"):
            license_classifiers.append(classifier)
        else:
            other_classifiers.append(classifier)
    return license_classifiers, other_classifiers


def get_keywords(metainfo):
    """
    Return a list of keywords found in a ``metainfo`` object or mapping.
    """
    keywords = []
    kws = get_attribute(metainfo, "Keywords") or []
    if kws:
        if isinstance(kws, str):
            kws = kws.split(",")
        elif isinstance(kws, (list, tuple)):
            pass
        else:
            kws = [repr(kws)]
        kws = [k.strip() for k in kws if k and k.strip()]
        keywords.extend(kws)

    # we are calling this again and ignoring licenses
    _, other_classifiers = get_classifiers(metainfo)
    keywords.extend(other_classifiers)
    return keywords


def get_parties(metainfo):
    """
    Return a list of parties found in a ``metainfo`` object or mapping.
    """
    parties = []

    author = get_attribute(metainfo, "Author")
    author_email = get_attribute(metainfo, "Author-email")
    if author or author_email:
        parties.append(
            models.Party(
                type=models.party_person,
                name=author or None,
                role="author",
                email=author_email or None,
            )
        )

    maintainer = get_attribute(metainfo, "Maintainer")
    maintainer_email = get_attribute(metainfo, "Maintainer-email")
    if maintainer or maintainer_email:
        parties.append(
            models.Party(
                type=models.party_person,
                name=maintainer or None,
                role="maintainer",
                email=maintainer_email or None,
            )
        )

    return parties


def get_setup_py_dependencies(setup_args):
    """
    Return a list of DependentPackage found in a ``setup_args`` mapping of
    setup.py arguments or an empty list.
    """
    dependencies = []

    python_requires = setup_args.get("python_requires")
    if python_requires:
        # FIXME: handle python_requires = >=3.6.*
        pass

    install_requires = setup_args.get("install_requires")
    dependencies.extend(get_requires_dependencies(install_requires, default_scope="install"))

    tests_requires = setup_args.get("tests_requires")
    dependencies.extend(get_requires_dependencies(tests_requires, default_scope="tests"))

    setup_requires = setup_args.get("setup_requires")
    dependencies.extend(get_requires_dependencies(setup_requires, default_scope="setup"))

    extras_require = setup_args.get("extras_require", {})
    for scope, requires in extras_require.items():
        dependencies.extend(get_requires_dependencies(requires, default_scope=scope))

    return dependencies


def is_simple_requires(requires):
    """
    Return True if ``requires`` is a sequence of strings.
    """
    return requires and isinstance(requires, list) and all(isinstance(i, str) for i in requires)


def get_dist_dependencies(dist):
    """
    Return a list of DependentPackage found in a ``dist`` Distribution object or
    an empty list.
    """
    # we treat extras as scopes
    # TODO: use these for verification?
    scopes = dist.metadata.get_all("Provides-Extra") or []
    return get_requires_dependencies(requires=dist.requires)


def get_requires_dependencies(requires, default_scope="install"):
    """
    Return a list of DependentPackage found in a ``requires`` list of
    requirement strings or an empty list.
    """
    if not is_simple_requires(requires):
        # FIXME: when does this happen? should we log this?
        return []
    dependent_packages = []
    for req in requires or []:
        req = Requirement(req)
        name = canonicalize_name(req.name)
        is_resolved = False
        purl = PackageURL(type="pypi", name=name)
        # note: packaging.requirements.Requirement.specifier is a
        # packaging.specifiers.SpecifierSet object and a SpecifierSet._specs is
        # a set of either: packaging.specifiers.Specifier or
        # packaging.specifiers.LegacySpecifier and each of these have a
        # .operator and .version property
        # a packaging.specifiers.SpecifierSet
        specifiers_set = req.specifier  # a list of packaging.specifiers.Specifier
        specifiers = specifiers_set._specs
        requirement = None
        if specifiers:
            # SpecifierSet stringifies to comma-separated sorted Specifiers
            requirement = str(specifiers_set)
            # are we pinned e.g. resolved? this is true if we have a single
            # equality specifier
            if len(specifiers) == 1:
                specifier = list(specifiers)[0]
                if specifier.operator in ("==", "==="):
                    is_resolved = True
                    purl = purl._replace(version=specifier.version)

        # we use the extra as scope if avialble
        scope = get_extra(req.marker) or default_scope

        dependent_packages.append(
            models.DependentPackage(
                purl=purl.to_string(),
                scope=scope,
                is_runtime=True,
                is_optional=False,
                is_resolved=is_resolved,
                extracted_requirement=requirement,
            )
        )

    return dependent_packages


def get_extra(marker):
    """
    Return the "extra" value of a ``marker`` requirement Marker or None.
    """
    if not marker or not isinstance(marker, markers.Marker):
        return

    marks = getattr(marker, "_markers", [])

    for mark in marks:
        # filter for variable(extra) == value tuples of (Variable, Op, Value)
        if not isinstance(mark, tuple) and not len(mark) == 3:
            continue

        variable, operator, value = mark

        if (
            isinstance(variable, markers.Variable)
            and variable.value == "extra"
            and isinstance(operator, markers.Op)
            and operator.value == "=="
            and isinstance(value, markers.Value)
        ):
            return value.value


def get_dparse2_supported_file_name(file_name):
    """
    Return the file_name if this is supported or None given a `file_name`
    string.
    """
    # this is kludgy but the upstream data structure and API needs this
    dfile_names = (
        "Pipfile.lock",
        "Pipfile",
        "conda.yml",
        "setup.cfg",
    )

    for dfile_name in dfile_names:
        if file_name.endswith(dfile_name):
            return file_name


def parse_with_dparse2(location, file_name=None):
    """
    Return a list of DependentPackage built from a dparse2-supported dependency
    manifest such as Conda manifest or Pipfile.lock files, or return an empty
    list.
    """
    with open(location) as f:
        content = f.read()

    dep_file = dparse2.parse(content, file_name=file_name)
    if not dep_file:
        return []

    dependent_packages = []

    for dependency in dep_file.dependencies:
        requirement = dependency.name
        is_resolved = False
        purl = PackageURL(type="pypi", name=dependency.name)

        # note: dparse2.dependencies.Dependency.specs comes from
        # packaging.requirements.Requirement.specifier
        # which in turn is a packaging.specifiers.SpecifierSet objects
        # and a SpecifierSet._specs is a set of either:
        # packaging.specifiers.Specifier or packaging.specifiers.LegacySpecifier
        # and each of these have a .operator and .version property

        # a packaging.specifiers.SpecifierSet
        specifiers_set = dependency.specs
        # a list of packaging.specifiers.Specifier
        specifiers = specifiers_set._specs

        if specifiers:
            # SpecifierSet stringifies to comma-separated sorted Specifiers
            requirement = str(specifiers_set)
            # are we pinned e.g. resolved?
            if len(specifiers) == 1:
                specifier = list(specifiers)[0]
                if specifier.operator in ("==", "==="):
                    is_resolved = True
                    purl = purl._replace(version=specifier.version)

        dependent_packages.append(
            models.DependentPackage(
                purl=purl.to_string(),
                # are we always this scope? what if we have requirements-dev.txt?
                scope="install",
                is_runtime=True,
                is_optional=False,
                is_resolved=is_resolved,
                extracted_requirement=requirement,
            )
        )

    return dependent_packages


def get_setup_py_args(location):
    """
    Return a mapping of arguments passed to a setup.py setup() function.
    """
    with open(location) as inp:
        setup_text = inp.read()

    setup_args = {}

    # Parse setup.py file and traverse the AST
    tree = ast.parse(setup_text)
    for statement in tree.body:
        # We only care about function calls or assignments to functions named
        # `setup` or `main`
        if not (
            isinstance(statement, (ast.Expr, ast.Call, ast.Assign))
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            # we also look for main as sometimes this is used instead of setup()
            and statement.value.func.id in ("setup", "main")
        ):
            continue

        # Process the arguments to the setup function
        for kw in getattr(statement.value, "keywords", []):
            arg_name = kw.arg

            if isinstance(kw.value, ast.Str):
                setup_args[arg_name] = kw.value.s

            elif isinstance(
                kw.value,
                (
                    ast.List,
                    ast.Tuple,
                    ast.Set,
                ),
            ):
                # We collect the elements of a list if the element
                # and tag function calls
                value = [elt.s for elt in kw.value.elts if not isinstance(elt, ast.Call)]
                setup_args[arg_name] = value

            # TODO:  what if isinstance(kw.value, ast.Dict)
            # or an expression like a call to version=get_version or version__version__

    return setup_args


def get_pypi_urls(name, version):
    """
    Return a mapping of computed Pypi URLs for this package
    """
    api_data_url = None
    if name and version:
        api_data_url = f"https://pypi.org/pypi/{name}/{version}/json"
    else:
        api_data_url = name and f"https://pypi.org/pypi/{name}/json"

    repository_download_url = (
        name
        and version
        and f"https://pypi.org/packages/source/{name[0]}/{name}/{name}-{version}.tar.gz"
    )

    repository_homepage_url = name and f"https://pypi.org/project/{name}"

    return dict(
        repository_homepage_url=repository_homepage_url,
        repository_download_url=repository_download_url,
        api_data_url=api_data_url,
    )


def get_urls(metainfo, name, version, extra_data=None):
    """
    Return a mapping for URLs of this package:
    - as plain name/values for URL attributes known in PackageData
    - as a nested extra_data: mapping for other URLs (possibly updating extra_data if provided).
    """
    # Misc URLs to possibly track
    # Project-URL: Release notes
    # Project-URL: Release Notes
    # Project-URL: Changelog
    # Project-URL: Changes
    #
    # Project-URL: Further Documentation
    # Project-URL: Packaging tutorial
    # Project-URL: Docs
    # Project-URL: Docs: RTD
    # Project-URL: Documentation
    # Project-URL: Documentation (dev)
    # Project-URL: Wiki
    #
    # Project-URL: Chat
    # Project-URL: Chat: Gitter
    # Project-URL: Mailing lists
    # Project-URL: Twitter
    #
    # Project-URL: Travis CI
    # Project-URL: Coverage: codecov
    # Project-URL: CI
    # Project-URL: CI: Azure Pipelines
    # Project-URL: CI: Shippable
    #
    # Project-URL: Tidelift
    # Project-URL: Code of Conduct
    # Project-URL: Donate
    # Project-URL: Funding
    # Project-URL: Ko-fi
    # Project-URL: Twine documentation
    # Project-URL: Twine source
    # Project-URL: Say Thanks!

    extra_data = extra_data or {}
    urls = get_pypi_urls(name, version)

    def add_url(_url, _utype=None, _attribute=None):
        """
        Add ``_url`` to ``urls`` as _``_attribute`` or to ``extra_data`` as
        ``_utype`` if already defined or no ``_attribute`` is provided.
        """
        if _url:
            if _attribute and _attribute not in urls:
                urls[_attribute] = _url
            elif _utype:
                extra_data[_utype] = _url

    # get first as this is the most common one
    homepage_url = (
        get_attribute(metainfo, "Home-page")
        or get_attribute(metainfo, "url")
        or get_attribute(metainfo, "home")
    )
    add_url(homepage_url, _attribute="homepage_url")

    project_urls = (
        get_attribute(metainfo, "Project-URL", multiple=True)
        or get_attribute(metainfo, "project_urls")
        or []
    )

    for url in project_urls:
        utype, _, uvalue = url.partition(",")
        uvalue = uvalue.strip()
        utype = utype.strip()
        utypel = utype.lower()
        if utypel in (
            "tracker",
            "bug reports",
            "github: issues",
            "bug tracker",
            "issues",
            "issue tracker",
        ):
            add_url(url, _utype=utype, _attribute="bug_tracking_url")

        elif utypel in (
            "source",
            "source code",
            "code",
        ):
            add_url(url, _utype=utype, _attribute="code_view_url")

        elif utypel in ("github", "gitlab", "github: repo", "repository"):
            add_url(url, _utype=utype, _attribute="vcs_url")

        elif utypel in (
            "website",
            "homepage",
            "home",
        ):
            add_url(url, _utype=utype, _attribute="homepage_url")

        else:
            add_url(url, _utype=utype)

    # FIXME: this may not be the actual correct package download URL, so for now
    # we incorrectly set this as the vcs_url
    download_url = get_attribute(metainfo, "Download-URL")
    add_url(download_url, _utype="Download-URL", _attribute="vcs_url")

    if extra_data:
        urls["extra_data"] = extra_data
    return urls


def find_pattern(location, pattern):
    """
    Search the file at `location` for a patern regex on a single line and return
    this or None if not found. Reads the supplied location as text without
    importing it.

    Code inspired and heavily modified from:
    https://github.com/pyserial/pyserial/blob/d867871e6aa333014a77498b4ac96fdd1d3bf1d8/setup.py#L34
    SPDX-License-Identifier: BSD-3-Clause
    (C) 2001-2020 Chris Liechti <cliechti@gmx.net>
    """
    with io.open(location, encoding="utf8") as fp:
        content = fp.read()

    match = re.search(pattern, content)
    if match:
        return match.group(1).strip()


def find_dunder_version(location):
    """
    Return a "dunder" __version__ string or None from searching the module file
    at `location`.

    Code inspired and heavily modified from:
    https://github.com/pyserial/pyserial/blob/d867871e6aa333014a77498b4ac96fdd1d3bf1d8/setup.py#L34
    SPDX-License-Identifier: BSD-3-Clause
    (C) 2001-2020 Chris Liechti <cliechti@gmx.net>
    """
    pattern = re.compile(r"^__version__\s*=\s*['\"]([^'\"]*)['\"]", re.MULTILINE)
    match = find_pattern(location, pattern)
    if TRACE:
        logger_debug("find_dunder_version:", "location:", location, "match:", match)
    return match


def find_plain_version(location):
    """
    Return a plain version attribute string or None from searching the module
    file at `location`.
    """
    pattern = re.compile(r"^version\s*=\s*['\"]([^'\"]*)['\"]", re.MULTILINE)
    match = find_pattern(location, pattern)
    if TRACE:
        logger_debug("find_plain_version:", "location:", location, "match:", match)
    return match


def find_setup_py_dunder_version(location):
    """
    Return a "dunder" __version__ expression string used as a setup(version)
    argument or None from searching the setup.py file at `location`.

    For instance:
        setup(
            version=six.__version__,
        ...
    would return six.__version__.

    Code inspired and heavily modified from:
    https://github.com/pyserial/pyserial/blob/d867871e6aa333014a77498b4ac96fdd1d3bf1d8/setup.py#L34
    SPDX-License-Identifier: BSD-3-Clause
    (C) 2001-2020 Chris Liechti <cliechti@gmx.net>
    """
    pattern = re.compile(r"^\s*version\s*=\s*(.*__version__)", re.MULTILINE)
    match = find_pattern(location, pattern)
    if TRACE:
        logger_debug("find_setup_py_dunder_version:", "location:", location, "match:", match)
    return match


def detect_version_attribute(setup_location):
    """
    Return a detected version from a setup.py file at `location` if used as in
    a version argument of the setup() function.
    Also search for neighbor files for __version__ and common patterns.
    """
    # search for possible dunder versions here and elsewhere
    setup_version_arg = find_setup_py_dunder_version(setup_location)
    setup_py__version = find_dunder_version(setup_location)
    if TRACE:
        logger_debug("    detect_dunder_version:", "setup_location:", setup_location)
        logger_debug(
            "    setup_version_arg:",
            repr(setup_version_arg),
        )
        logger_debug(
            "    setup_py__version:",
            repr(setup_py__version),
        )

    if setup_version_arg == "__version__" and setup_py__version:
        version = setup_py__version or None
        if TRACE:
            logger_debug("    detect_dunder_version: A:", version)
        return version

    # here we have a more complex __version__ location
    # we start by adding the possible paths and file name
    # and we look at these in sequence

    candidate_locs = []

    if setup_version_arg and "." in setup_version_arg:
        segments = setup_version_arg.split(".")[:-1]
    else:
        segments = []

    special_names = (
        "__init__.py",
        "__main__.py",
        "__version__.py",
        "__about__.py",
        "__version.py",
        "_version.py",
        "version.py",
        "VERSION.py",
        "package_data.py",
    )

    setup_py_dir = fileutils.parent_directory(setup_location)
    src_dir = os.path.join(setup_py_dir, "src")
    has_src = os.path.exists(src_dir)

    if segments:
        for n in special_names:
            candidate_locs.append(segments + [n])
        if has_src:
            for n in special_names:
                candidate_locs.append(["src"] + segments + [n])

        if len(segments) > 1:
            heads = segments[:-1]
            tail = segments[-1]
            candidate_locs.append(heads + [tail + ".py"])
            if has_src:
                candidate_locs.append(["src"] + heads + [tail + ".py"])

        else:
            seg = segments[0]
            candidate_locs.append([seg + ".py"])
            if has_src:
                candidate_locs.append(["src", seg + ".py"])

    candidate_locs = [
        os.path.join(setup_py_dir, *cand_loc_segs) for cand_loc_segs in candidate_locs
    ]

    for fl in get_module_scripts(
        location=setup_py_dir,
        max_depth=4,
        interesting_names=special_names,
    ):
        candidate_locs.append(fl)

    if TRACE:
        for loc in candidate_locs:
            logger_debug("    can loc:", loc)

    version = detect_version_in_locations(
        candidate_locs=candidate_locs, detector=find_dunder_version
    )

    if version:
        return version

    return detect_version_in_locations(
        candidate_locs=candidate_locs,
        detector=find_plain_version,
    )


def detect_version_in_locations(candidate_locs, detector=find_plain_version):
    """
    Return the first version found in a location from the `candidate_locs` list
    using the `detector` callable. Return None if no version is found.
    """
    for loc in candidate_locs:
        if not os.path.exists(loc):
            continue

        if TRACE:
            logger_debug("detect_version_in_locations:", "loc:", loc)

        # here the file exists try to get a dunder version
        version = detector(loc)

        if TRACE:
            logger_debug(
                "detect_version_in_locations:",
                "detector",
                detector,
                "version:",
                version,
            )

        if version:
            return version


def get_module_scripts(location, max_depth=1, interesting_names=()):
    """
    Yield interesting Python script paths that have a name in
    `interesting_names` by walking the `location` directory recursively up to
    `max_depth` path segments extending from the root `location`.
    """

    location = location.rstrip(os.path.sep)
    current_depth = max_depth
    for top, _dirs, files in os.walk(location):
        if current_depth == 0:
            break
        for f in files:
            if f in interesting_names:
                path = os.path.join(top, f)
                if TRACE:
                    logger_debug("get_module_scripts:", "path", path)
                yield path

        current_depth -= 1