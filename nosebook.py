import logging
import os
import re
import json
from copy import copy

try:
    # py3
    from queue import Empty

    def isstr(s):
        return isinstance(s, str)
except ImportError:
    # py2
    from Queue import Empty

    def isstr(s):
        return isinstance(s, basestring)  # noqa

from unittest import TestCase

from nose.plugins import Plugin


try:
    from ipykernel.tests import utils
    from nbformat.converter import convert
    from nbformat.reader import reads
    IPYTHON_VERSION = 4
except ImportError:
    from IPython.kernel.tests import utils
    try:
        from IPython.nbformat.converter import convert
        from IPython.nbformat.reader import reads
        IPYTHON_VERSION = 3
    except ImportError:
        from IPython.nbformat.convert import convert
        from IPython.nbformat.reader import reads
        IPYTHON_VERSION = 2

NBFORMAT_VERSION = 4

__version__ = "0.4.0"

log = logging.getLogger("nose.plugins.nosebook")


class NosebookTwo(object):
    """
    Implement necessary functions against the IPython 2.x API
    """

    def newKernel(self, nb):
        """
        generate a new kernel
        """
        manager, kernel = utils.start_new_kernel()
        return kernel


class NosebookThree(object):
    """
    Implement necessary functions against the IPython 3.x API
    """
    def newKernel(self, nb):
        """
        generate a new kernel
        """
        manager, kernel = utils.start_new_kernel(
            kernel_name=nb.metadata.kernelspec.name
        )
        return kernel


if IPYTHON_VERSION == 2:
    NosebookVersion = NosebookTwo
else:
    NosebookVersion = NosebookThree


def dump_canonical(obj):
    return json.dumps(obj, indent=2, sort_keys=True)


class Nosebook(NosebookVersion, Plugin):
    """
    A nose plugin for discovering and executing IPython notebook cells
    as tests
    """
    name = "nosebook"

    def options(self, parser, env=os.environ):
        """
        advertise options
        """
        self.testMatchPat = env.get('NOSEBOOK_TESTMATCH',
                                    r'.*[Tt]est.*\.ipynb$')

        parser.add_option(
            "--nosebook-match",
            action="store",
            dest="nosebookTestMatch",
            metavar="REGEX",
            help="Notebook files that match this regular expression are "
                 "considered tests.  "
                 "Default: %s [NOSEBOOK_TESTMATCH]" % self.testMatchPat,
            default=self.testMatchPat
        )

        super(Nosebook, self).options(parser, env=env)

    def configure(self, options, conf):
        """
        apply configured options
        """
        super(Nosebook, self).configure(options, conf)

        self.testMatch = re.compile(options.nosebookTestMatch).match

    def wantModule(self, *args, **kwargs):
        """
        we don't handle actual code modules!
        """
        return False

    def _readnb(self, filename):
        with open(filename) as f:
            return reads(f.read())

    def readnb(self, filename):
        try:
            nb = self._readnb(filename)
        except Exception as err:
            log.info("could not be parse as a notebook %s\n%s",
                     filename,
                     err)
            return False

        return convert(nb, NBFORMAT_VERSION)

    def codeCells(self, nb):
        for cell in nb.cells:
            if cell.cell_type == "code":
                yield cell

    def wantFile(self, filename):
        """
        filter files to those that match nosebook-match
        """
        log.info("considering %s", filename)

        if self.testMatch(filename) is None:
            return False

        nb = self.readnb(filename)

        for cell in self.codeCells(nb):
            return True

        log.info("no `code` cells in %s", filename)

        return False

    def loadTestsFromFile(self, filename):
        """
        find all tests in a notebook.
        """
        nb = self.readnb(filename)

        kernel = self.newKernel(nb)

        for cell_idx, cell in enumerate(self.codeCells(nb)):
            yield NoseCellTestCase(
                cell,
                cell_idx,
                kernel,
                filename=filename,
            )


class NoseCellTestCase(TestCase):
    """
    A test case for a single cell.
    """
    STRIP_KEYS = ["execution_count", "traceback", "prompt_number", "source"]

    def __init__(self, cell, cell_idx, kernel, *args, **kwargs):
        """
        initialize this cell as a test
        """

        self.cell = self.sanitizeCell(cell)
        self.cell_idx = cell_idx
        self.filename = kwargs.pop("filename", "")

        self.kernel = kernel
        self.iopub = self.kernel.iopub_channel

        self.runTest.__func__.__doc__ = self.id()

        super(NoseCellTestCase, self).__init__(*args, **kwargs)

    def id(self):
        return "%s#%s" % (self.filename, self.cell_idx)

    def cellCode(self):
        if hasattr(self.cell, "source"):
            return self.cell.source
        return self.cell.input

    def runTest(self):
        self.kernel.execute(self.cellCode())

        outputs = []
        msg = None

        while self.shouldContinue(msg):
            try:
                msg = self.iopub.get_msg(block=True, timeout=1)
            except Empty:
                continue

            if msg['msg_type'] == 'error':
                log.debug(msg['content']['traceback'])
                raise Exception("Error during cell evaluation\n"
                                "Source:\n%s\n%s\n%s" %
                                (self.cell.source,
                                 msg['content']['ename'],
                                 msg['content']['evalue']))
            #else:
            #    log.debug("msg=\n%s", pprint.pformat(msg))

    def stripKeys(self, d):
        """
        remove keys from STRIP_KEYS to ensure comparability
        """
        for key in self.STRIP_KEYS:
            d.pop(key, None)
        return d

    def sanitizeCell(self, cell):
        """
        remove non-reproducible things
        """
        for output in cell.outputs:
            self.stripKeys(output)
        return cell

    def shouldContinue(self, msg):
        """
        determine whether the current message is the last for this cell
        """
        if msg is None:
            return True

        return not (msg["msg_type"] == "status" and
                    msg["content"]["execution_state"] == "idle")
