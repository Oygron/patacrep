"""Song management."""

import errno
import hashlib
import jinja2
import logging
import os
import pickle
import re

from patacrep.authors import process_listauthors
from patacrep import files, encoding

LOGGER = logging.getLogger(__name__)

def cached_name(datadir, filename):
    """Return the filename of the cache version of the file."""
    fullpath = os.path.abspath(os.path.join(datadir, '.cache', filename))
    directory = os.path.dirname(fullpath)
    try:
        os.makedirs(directory)
    except OSError as error:
        if error.errno == errno.EEXIST and os.path.isdir(directory):
            pass
        else:
            raise
    return fullpath

class DataSubpath(object):
    """A path divided in two path: a datadir, and its subpath.

    - This object can represent either a file or directory.
    - If the datadir part is the empty string, it means that the represented
      path does not belong to a datadir.
    """

    def __init__(self, datadir, subpath):
        if os.path.isabs(subpath):
            self.datadir = ""
        else:
            self.datadir = datadir
        self.subpath = subpath

    def __str__(self):
        return os.path.join(self.datadir, self.subpath)

    @property
    def fullpath(self):
        """Return the full path represented by self."""
        return os.path.join(self.datadir, self.subpath)

    def clone(self):
        """Return a cloned object."""
        return DataSubpath(self.datadir, self.subpath)

    def join(self, path):
        """Join "path" argument to self path.

        Return self for commodity.
        """
        self.subpath = os.path.join(self.subpath, path)
        return self

# pylint: disable=too-many-instance-attributes
class Song:
    """Song (or song metadata)

    This class represents a song, bound to a file.

    - It can parse the file given in arguments.
    - It can render the song as some code (LaTeX, chordpro, depending on subclasses implemetation).
    - Its content is cached, so that if the file has not been changed, the
      file is not parsed again.

    This class is inherited by classes implementing song management for
    several file formats. Those subclasses must implement:
    - `parse()` to parse the file;
    - `render()` to render the song as code.
    """

    # Version format of cached song. Increment this number if we update
    # information stored in cache.
    CACHE_VERSION = 2

    # List of attributes to cache
    cached_attributes = [
        "titles",
        "unprefixed_titles",
        "cached",
        "data",
        "subpath",
        "languages",
        "authors",
        "_filehash",
        "_version",
        ]

    def __init__(self, subpath, config, *, datadir=None):
        if datadir is None:
            self.datadir = ""
        else:
            self.datadir = datadir
        self.fullpath = os.path.join(self.datadir, subpath)
        self.encoding = config["encoding"]
        self.config = config

        if datadir:
            # Only songs in datadirs are cached
            self._filehash = hashlib.md5(
                open(self.fullpath, 'rb').read()
                ).hexdigest()
            if os.path.exists(cached_name(datadir, subpath)):
                try:
                    cached = pickle.load(open(
                        cached_name(datadir, subpath),
                        'rb',
                        ))
                    if (
                            cached['_filehash'] == self._filehash
                            and cached['_version'] == self.CACHE_VERSION
                    ):
                        for attribute in self.cached_attributes:
                            setattr(self, attribute, cached[attribute])
                        return
                except: # pylint: disable=bare-except
                    LOGGER.warning("Could not use cached version of {}.".format(
                        self.fullpath
                        ))

        # Data extraction from the latex song
        self.titles = []
        self.data = {}
        self.cached = None
        self._parse(config)

        # Post processing of data
        self.subpath = subpath
        self.unprefixed_titles = [
            unprefixed_title(
                title,
                config['titleprefixwords']
                )
            for title
            in self.titles
            ]
        self.authors = process_listauthors(
            self.authors,
            **config.get("_compiled_authwords", {})
            )

        # Cache management
        self._version = self.CACHE_VERSION
        self._write_cache()

    def _write_cache(self):
        """If relevant, write a dumbed down version of self to the cache."""
        if self.datadir:
            cached = {}
            for attribute in self.cached_attributes:
                cached[attribute] = getattr(self, attribute)
            pickle.dump(
                cached,
                open(cached_name(self.datadir, self.subpath), 'wb'),
                protocol=-1
                )

    def __repr__(self):
        return repr((self.titles, self.data, self.fullpath))

    def render(self, output_format, output=None, *args, **kwargs):
        """Return the code rendering this song.

        Arguments:
        - output_format: Format of the output file (latex, chordpro...)
        - output: Name of the output file, or `None` if irrelevant.
        """
        method = "render_{}".format(output_format)
        if hasattr(self, method):
            return getattr(self, method)(output, *args, **kwargs)
        raise NotImplementedError()

    def _parse(self, config): # pylint: disable=no-self-use
        """Parse song.

        It set the following attributes:

        - titles: the list of (raw) titles. This list will be processed to
          remove prefixes.
        - languages: the list of languages used in the song, as languages
          recognized by the LaTeX babel package.
        - authors: the list of (raw) authors. This list will be processed to
          'clean' it (see function :func:`patacrep.authors.processauthors`).
        - data: song metadata. Used (among others) to sort the songs.
        - cached: additional data that will be cached. Thus, data stored in
          this attribute must be picklable.
        """
        raise NotImplementedError()

    def get_datadirs(self, subdir=None):
        """Return an iterator of existing datadirs (with eventually a subdir)
        """
        for directory in self.config['datadir']:
            fullpath = os.path.join(directory, subdir)
            if os.path.isdir(fullpath):
                yield fullpath

    def search_file(self, filename, extensions=None, directories=None):
        """Search for a file name.

        :param str filename: The name, as provided in the chordpro file (with or without extension).
        :param list extensions: Possible extensions (with '.'). Default is no extension.
        :param iterator directories: Other directories where to search for the file
                                The directory where the Song file is stored is added to the list.

        Returns None if nothing found.

        This function can also be used as a preprocessor for a renderer: for
        instance, it can compile a file, place it in a temporary folder, and
        return the path to the compiled file.
        """
        if extensions is None:
            extensions = ['']
        if directories is None:
            directories = self.config['datadir']

        songdir = os.path.dirname(self.fullpath)

        for directory in [songdir] + list(directories):
            for extension in extensions:
                fullpath = os.path.join(directory, filename + extension)
                if os.path.isfile(fullpath):
                    return os.path.abspath(fullpath)
        return None

    def search_image(self, filename, none_if_not_found=False):
        """Search for an image file"""
        filepath = self.search_file(
            filename,
            ['', '.jpg', '.png'],
            self.get_datadirs('img'),
            )
        return filepath if none_if_not_found or filepath else filename

    def search_partition(self, filename, none_if_not_found=False):
        """Search for a lilypond file"""
        filepath = self.search_file(filename, ['', '.ly'])
        return filepath if none_if_not_found or filepath else filename

def unprefixed_title(title, prefixes):
    """Remove the first prefix of the list in the beginning of title (if any).
    """
    for prefix in prefixes:
        match = re.compile(r"^(%s)\b\s*(.*)$" % prefix, re.LOCALE).match(title)
        if match:
            return match.group(2)
    return title