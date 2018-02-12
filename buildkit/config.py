# -*- coding: UTF-8 -*-

# Copyright (c) 2018 The ungoogled-chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""
Build configuration generation implementation
"""

import abc
import configparser
import collections
import itertools
import re
import shutil

from .common import ENCODING, CONFIG_BUNDLES_DIR, get_logger, get_resources_dir
from .third_party import schema

# Constants

PRUNING_LIST = "pruning.list"
DOMAIN_REGEX_LIST = "domain_regex.list"
DOMAIN_SUBSTITUTION_LIST = "domain_substitution.list"
EXTRA_DEPS_INI = "extra_deps.ini"
GN_FLAGS_MAP = "gn_flags.map"
BASEBUNDLEMETA_INI = "basebundlemeta.ini"
PATCH_ORDER_LIST = "patch_order.list"
PATCHES_DIR = "patches"
VERSION_INI = "version.ini"

# Helpers for third_party.schema

def _DictCast(data): #pylint: disable=invalid-name
    return schema.And(schema.Use(dict), data)

def _IniSchema(data): #pylint: disable=invalid-name
    return _DictCast({configparser.DEFAULTSECT: object, **data})

# Classes

class _ConfigABC(abc.ABC):
    """Abstract base class for configuration files or directories"""

    def __init__(self, path, name=None):
        """
        Initializes the config class.

        path is a pathlib.Path to a config file or directory.
        name is a type identifier and the actual file or directory name.

        Raises FileNotFoundError if path does not exist.
        """
        if not path.exists():
            raise FileNotFoundError(str(path))
        self.path = path
        if name:
            self.name = name
        else:
            self.name = path.name
        # List of paths to inherit from, ordered from left to right.
        self._path_order = collections.deque()
        self._path_order.appendleft(path)

    def _check_path_add(self, path):
        if path in self._path_order:
            return False
        if not path.exists():
            get_logger().error('Unable to add path for "%s"', self.name)
            raise FileNotFoundError(str(path))
        return True

    def update_first_path(self, path):
        """
        Sets a config path as the new first path to be processed, if it is not already known.

        Returns True if the config path was added,
        False if the config path is already known.

        Raises FileNotFoundError if path does not exist
        """
        if self._check_path_add(path):
            self._path_order.appendleft(path)
            return True
        return False

    def update_last_path(self, path):
        """
        Sets a config path as the new last path to be processed, if it is not already known.

        Returns True if the config path was added,
        False if the config path is already known.

        Raises FileNotFoundError if path does not exist
        """
        if self._check_path_add(path):
            self._path_order.append(path)
            return True
        return False

    @abc.abstractmethod
    def _parse_data(self):
        """Parses and returns config data"""
        pass

    @property
    def _config_data(self):
        """Returns the parsed config data"""
        parsed_data = self._parse_data()
        if parsed_data is None:
            # Assuming no parser intentionally returns None
            get_logger().error('Got None from parser of "%s"', self.name)
            raise TypeError('Got None from parser')
        return parsed_data

    @abc.abstractmethod
    def write(self, path):
        """Writes the config to path"""
        pass

class _CacheConfigMixin: #pylint: disable=too-few-public-methods
    """Mixin for _ConfigABC to cache parse output"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._read_cache = None

    @property
    def _config_data(self):
        """
        Returns the cached parsed config data.
        It parses and caches if the cash is not present.
        """
        if self._read_cache:
            return self._read_cache
        self._read_cache = super()._config_data
        return self._read_cache

class IniConfigFile(_CacheConfigMixin, _ConfigABC):
    """Represents an INI file"""

    _schema = schema.Schema(object) # Allow any INI by default

    def __getitem__(self, key):
        """
        Returns a section from the INI

        Raises KeyError if the section does not exist
        """
        return self._config_data[key]

    def __contains__(self, item):
        """
        Returns True if item is a name of a section; False otherwise.
        """
        return self._config_data.has_section(item)

    def __iter__(self):
        """Returns an iterator over the section names"""
        return iter(self._config_data.sections())

    def _parse_data(self):
        """
        Returns a parsed INI file.
        Raises third_party.schema.SchemaError if validation fails
        """
        parsed_ini = configparser.ConfigParser()
        for ini_path in self._path_order:
            with ini_path.open(encoding=ENCODING) as ini_file:
                parsed_ini.read_file(ini_file, source=str(ini_path))
        try:
            self._schema.validate(parsed_ini)
        except schema.SchemaError as exc:
            get_logger().error(
                'Merged INI files failed schema validation: %s', tuple(self._path_order))
            raise exc
        return parsed_ini

    def write(self, path):
        ini_parser = configparser.ConfigParser()
        ini_parser.read_dict(self._config_data)
        with path.open("w", encoding=ENCODING) as output_file:
            ini_parser.write(output_file)

class ListConfigFile(_ConfigABC):
    """Represents a simple newline-delimited list"""
    def __contains__(self, item):
        """Returns True if item is in the list; False otherwise"""
        return item in self._config_data

    def _line_generator(self):
        for list_path in self._path_order:
            with list_path.open(encoding=ENCODING) as list_file:
                line_iter = list_file.read().splitlines()
                yield from filter(len, line_iter)

    def __iter__(self):
        """Returns an iterator over the list items"""
        return iter(self._config_data)

    def _parse_data(self):
        """Returns a file object of the item's values"""
        return self._line_generator()

    def write(self, path):
        with path.open('w', encoding=ENCODING) as output_file:
            output_file.writelines(map(lambda x: '%s\n' % x, self._config_data))

class MappingConfigFile(_CacheConfigMixin, _ConfigABC):
    """Represents a simple string-keyed and string-valued dictionary"""
    def __contains__(self, item):
        """Returns True if item is a key in the mapping; False otherwise"""
        return item in self._config_data

    def __getitem__(self, key):
        """
        Returns the value associated with the key

        Raises KeyError if the key is not in the mapping
        """
        return self._config_data[key]

    def __iter__(self):
        """Returns an iterator over the keys"""
        return iter(self._config_data)

    def _parse_data(self):
        """Return a dictionary of the mapping of keys and values"""
        new_dict = dict()
        for mapping_path in self._path_order:
            with mapping_path.open(encoding=ENCODING) as mapping_file:
                for line in filter(len, mapping_file.read().splitlines()):
                    key, value = line.split('=')
                    new_dict[key] = value
        return new_dict

    def write(self, path):
        with path.open('w', encoding=ENCODING) as output_file:
            for item in self._config_data.items():
                output_file.write('%s=%s\n' % item)

class ConfigBundle(_CacheConfigMixin, _ConfigABC):
    """Represents a user or base config bundle"""

    @classmethod
    def from_base_name(cls, name):
        """
        Return a new ConfigBundle from a base config bundle name.

        Raises NotADirectoryError if the resources/ or resources/patches directories
        could not be found.
        Raises FileNotFoundError if the base config bundle name does not exist.
        Raises ValueError if there is an issue with the base bundle's or its
        dependencies' metadata
        """
        config_bundles_dir = get_resources_dir() / CONFIG_BUNDLES_DIR
        new_bundle = cls(config_bundles_dir / name)
        pending_explore = collections.deque()
        pending_explore.appendleft(name)
        known_names = set()
        while pending_explore:
            base_bundle_name = pending_explore.pop()
            if base_bundle_name in known_names:
                raise ValueError('Duplicate base config bundle dependency "{}"'.format(
                    base_bundle_name))
            known_names.add(base_bundle_name)
            basebundlemeta = BaseBundleMetaIni(
                config_bundles_dir / base_bundle_name / BASEBUNDLEMETA_INI)
            for dependency_name in basebundlemeta.depends:
                if new_bundle.update_first_path(config_bundles_dir / dependency_name):
                    pending_explore.appendleft(dependency_name)
        try:
            new_bundle.patches.set_patches_dir(get_resources_dir() / PATCHES_DIR)
        except KeyError:
            pass # Don't do anything if patch_order does not exist
        return new_bundle

    def get_dependencies(self):
        """
        Returns a tuple of dependencies for the config bundle, in descending order of inheritance.
        """
        return (x.name for x in tuple(self._path_order)[:-1])

    def __getitem__(self, key):
        """
        Returns the config file with the given name.

        Raises KeyError if the file is not found.
        Raises ValueError if the config is malformed.
        """
        return self._config_data[key]

    def __contains__(self, item):
        """
        Checks if a config file name exists.

        Raises ValueError if the config bundle is malformed.
        """
        return item in self._config_data

    def _parse_data(self):
        """
        Returns a dictionary of config file names to their respective objects.

        Raises ValueError if the config bundle contains unknown files.
        """
        file_dict = dict()
        for directory in self._path_order:
            for config_path in directory.iterdir():
                if config_path.name in file_dict:
                    file_dict[config_path.name].update_last_path(config_path)
                else:
                    try:
                        config_class = _FILE_DEF[config_path.name]
                    except KeyError:
                        logger = get_logger()
                        logger.error('Unknown file type at "%s"', config_path)
                        logger.error('Config directory "%s" has unknown files', directory.name)
                        raise ValueError(
                            'Unknown files in config bundle: {}'.format(directory))
                    if config_class:
                        file_dict[config_path.name] = config_class(config_path)
        return file_dict

    def write(self, path):
        """
        Writes a copy of this config bundle to a new directory specified by path.

        Raises FileExistsError if the directory already exists.
        Raises ValueError if the config bundle is malformed.
        """
        path.mkdir(parents=True)
        for config_file in self._config_data.values():
            config_file.write(path / config_file.name)

    @property
    def pruning(self):
        """Property to access pruning.list config file"""
        return self._config_data[PRUNING_LIST]

    @property
    def domain_regex(self):
        """Property to access domain_regex.list config file"""
        return self._config_data[DOMAIN_REGEX_LIST]

    @property
    def domain_substitution(self):
        """Property to access domain_substitution.list config file"""
        return self._config_data[DOMAIN_SUBSTITUTION_LIST]

    @property
    def extra_deps(self):
        """Property to access extra_deps.ini config file"""
        return self._config_data[EXTRA_DEPS_INI]

    @property
    def gn_flags(self):
        """Property to access gn_flags.map config file"""
        return self._config_data[GN_FLAGS_MAP]

    @property
    def patches(self):
        """Property to access patch_order.list and patches"""
        return self._config_data[PATCH_ORDER_LIST]

    @property
    def version(self):
        """Property to access version.ini config file"""
        return self._config_data[VERSION_INI]

class BaseBundleMetaIni(IniConfigFile):
    """Represents basebundlemeta.ini files"""

    _schema = schema.Schema(_IniSchema({
        'basebundle': _DictCast({
            'display_name': schema.And(str, len),
            schema.Optional('depends'): schema.And(str, len),
        })
    }))

    @property
    def display_name(self):
        """
        Returns the display name of the base bundle
        """
        return self['basebundle']['display_name']

    @property
    def depends(self):
        """
        Returns an iterable of the dependencies defined in the metadata.
        Parents are ordered in increasing precedence.
        """
        if 'depends' in self['basebundle']:
            return [x.strip() for x in self['basebundle']['depends'].split(',')]
        else:
            return tuple()

class DomainRegexList(ListConfigFile):
    """Representation of a domain_regex_list file"""
    _regex_pair_tuple = collections.namedtuple('DomainRegexPair', ('pattern', 'replacement'))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Dictionary of encoding to compiled and encoded regex pairs
        self._compiled_regex = dict()

    @staticmethod
    def _compile_encode_regex(line, encoding, enclosing_tuple):
        pattern, replacement = line.encode(encoding).split('#'.encode(encoding))
        return enclosing_tuple(re.compile(pattern), replacement)

    def get_regex_pairs(self, encoding):
        """Returns a tuple of compiled regex pairs"""
        if encoding not in self._compiled_regex:
            self._compiled_regex[encoding] = tuple(map(
                self._compile_encode_regex,
                self,
                itertools.repeat(encoding),
                itertools.repeat(self._regex_pair_tuple)))
        return self._compiled_regex[encoding]

class ExtraDepsIni(IniConfigFile):
    """Representation of an extra_deps.ini file"""

    _hashes = ('md5', 'sha1', 'sha256', 'sha512')
    _required_keys = ('version', 'url', 'download_name')
    _optional_keys = ('strip_leading_dirs')
    _passthrough_properties = (*_required_keys, *_optional_keys)

    _schema = schema.Schema(_IniSchema({
        schema.And(str, len): _DictCast({
            **{x: schema.And(str, len) for x in _required_keys},
            **{schema.Optional(x): schema.And(str, len) for x in _optional_keys},
            schema.Or(*_hashes): schema.And(str, len),
        })
    }))

    class _ExtraDepsSection: #pylint: disable=too-few-public-methods
        def __init__(self, section_dict, passthrough_properties, hashes):
            self._section_dict = section_dict
            self._passthrough_properties = passthrough_properties
            self._hashes = hashes

        def __getattr__(self, name):
            if name in self._passthrough_properties:
                return self._section_dict.get(name, fallback=None)
            elif name == 'hashes':
                hashes_dict = dict()
                for hash_name in self._hashes:
                    value = self._section_dict.get(hash_name, fallback=None)
                    if value:
                        hashes_dict[hash_name] = value
                return hashes_dict

    def __getitem__(self, section):
        """
        Returns an object with keys as attributes and
        values already pre-processed strings
        """
        return self._ExtraDepsSection(
            self._config_data[section], self._passthrough_properties,
            self._hashes)

class PatchesConfig(ListConfigFile):
    """Representation of patch_order and associated patches"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._patches_dir = None

    def set_patches_dir(self, path):
        """
        Sets the path to the directory containing the patches

        Raises NotADirectoryError if the path is not a directory or does not exist.
        """
        if not path.is_dir():
            raise NotADirectoryError(str(path))
        self._patches_dir = path

    def _get_patches_dir(self):
        """Returns the path to the patches directory"""
        if self._patches_dir is None:
            patches_dir = self.path.parent / "patches"
            if not patches_dir.is_dir():
                raise NotADirectoryError(str(patches_dir))
            self._patches_dir = patches_dir
        return self._patches_dir

    def patch_iter(self):
        """
        Returns an iterator of pathlib.Path to patch files in the proper order

        Raises NotADirectoryError if the patches directory is not a directory or does not exist
        """
        for relative_path in self:
            yield self._get_patches_dir() / relative_path

    def write(self, path):
        """Writes patch_order and patches/ directory to the same directory"""
        super().write(path)
        for relative_path in self:
            destination = path.parent / PATCHES_DIR / relative_path
            if not destination.parent.exists():
                destination.parent.mkdir(parents=True)
            shutil.copyfile(str(self._get_patches_dir() / relative_path), str(destination))

class VersionIni(IniConfigFile):
    """Representation of a version.ini file"""

    _schema = schema.Schema(_IniSchema({
        'version': _DictCast({
            'chromium_version': schema.And(str, len),
            'release_revision': schema.And(str, len),
            schema.Optional('release_extra'): schema.And(str, len),
        })
    }))

    @property
    def chromium_version(self):
        """Returns the Chromium version."""
        return self['version']['chromium_version']

    @property
    def release_revision(self):
        """Returns the release revision."""
        return self['version']['release_revision']

    @property
    def release_extra(self, fallback=None):
        """
        Return the release revision extra info, or returns fallback if it is not defined.
        """
        return self['version'].get('release_extra', fallback=fallback)

    @property
    def version_string(self):
        """
        Returns a version string containing all information in a Debian-like format.
        """
        result = '{}-{}'.format(self.chromium_version, self.release_revision)
        if self.release_extra:
            result += '~{}'.format(self.release_extra)
        return result

_FILE_DEF = {
    BASEBUNDLEMETA_INI: None, # This file has special handling, so ignore it
    PRUNING_LIST: ListConfigFile,
    DOMAIN_REGEX_LIST: DomainRegexList,
    DOMAIN_SUBSTITUTION_LIST: ListConfigFile,
    EXTRA_DEPS_INI: ExtraDepsIni,
    GN_FLAGS_MAP: MappingConfigFile,
    PATCH_ORDER_LIST: PatchesConfig,
    PATCHES_DIR: None, # Handled by PatchesConfig
    VERSION_INI: VersionIni,
}