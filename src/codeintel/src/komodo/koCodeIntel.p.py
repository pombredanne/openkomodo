#!python
# Copyright (c) 2004-2006 ActiveState Software Inc.
# See the file LICENSE.txt for licensing information.

"""The glue between the Komodo-independent 'codeintel' Python package
and Komodo's Code Intelligence functionality.
"""

import os
from os.path import basename, dirname
import sys
import string
import re
import threading
import logging
import types
import time
from pprint import pprint, pformat
import weakref
import operator
from bisect import bisect_left
import traceback
import shutil

from xpcom import components, nsError, ServerException, COMException
from xpcom._xpcom import PROXY_SYNC, PROXY_ALWAYS, PROXY_ASYNC, getProxyForObject
from xpcom.server import UnwrapObject, WrapObject
from koTreeView import TreeView
import uriparse
import directoryServiceUtils

from codeintel2.common import *
from codeintel2.manager import Manager
from codeintel2.environment import Environment
from codeintel2.util import indent
from codeintel2.indexer import ScanRequest, BatchUpdater, XMLParseRequest
from codeintel2.database.database import Database




#---- globals

log = logging.getLogger("koCodeIntel")
#log.setLevel(logging.DEBUG)



#---- component implementations

class KoCodeIntelEnvironment(Environment):
    """Provide a codeintel (runtime) Environment that uses koIUserEnviron and
    Komodo's prefs.
    """
    _com_interfaces_ = [components.interfaces.nsIObserver]
    _reg_clsid_ = "{94A112F1-97BA-4ADC-BE20-EAB712CBBB35}"
    _reg_contractid_ = "@activestate.com/koCodeIntelEnvironment;1"
    _reg_desc_ = "Komodo CodeIntel Environment"

    _ko_pref_name_from_ci_pref_name = {
        "python": "pythonDefaultInterpreter",
        "perl": "perlDefaultInterpreter",
        "php": "phpDefaultInterpreter",
        "ruby": "rubyDefaultInterpreter",
    }
    _ci_pref_name_from_ko_pref_name = dict((v,k)
       for k,v in _ko_pref_name_from_ci_pref_name.items())
    _converter_from_ko_pref_name = {
        "codeintel_selected_catalogs": eval,
    }
    _ko_pref_type_from_ko_pref_name = {
        # All prefs retrieved from the Komodo prefs system are presumed to be
        # string prefs, unless indicated here. Allowed values here are:
        # "string", "long", "boolean".
        "codeintel_max_recursive_dir_depth": "long",
    }

    def __init__(self, proj=None):
        Environment.__init__(self)

        if proj is None:
            self.name = "Default"
            self._unwrapped_proj_weakref = None
        else:
            self.name = proj.name
            self._unwrapped_proj_weakref = weakref.ref(UnwrapObject(proj))

        # 'self.prefsets' is the ordered list of prefsets in which to look
        # for prefs.
        prefSvc = components.classes["@activestate.com/koPrefService;1"]\
            .getService(components.interfaces.koIPrefService)
        self.prefsets = [
            # global prefset
            getProxyForObject(None, components.interfaces.koIPreference,
                              prefSvc.prefs, PROXY_ALWAYS | PROXY_SYNC)
        ]
        if proj is not None:
            # Typically this is the prefset for the project to which a
            # document belongs.
            self.prefsets.insert(0,
                getProxyForObject(None, components.interfaces.koIPreference,
                                  proj.prefset, PROXY_ALWAYS | PROXY_SYNC)
            )

        # <pref-name> -> <callback-id> -> <observer-callback>
        self._pref_observer_callbacks_from_name = {}

        userEnvSvc = components.classes["@activestate.com/koUserEnviron;1"]\
            .getService()
        self._userEnvSvc = getProxyForObject(None,
            components.interfaces.koIUserEnviron,
            userEnvSvc, PROXY_ALWAYS | PROXY_SYNC)
        langRegSvc = components.classes['@activestate.com/koLanguageRegistryService;1']\
            .getService(components.interfaces.koILanguageRegistryService)
        self._langRegSvc = getProxyForObject(None,
            components.interfaces.koILanguageRegistryService,
            langRegSvc, PROXY_ALWAYS | PROXY_SYNC)
        self._wrapped_self = WrapObject(self,
            components.interfaces.nsIObserver)


    def __repr__(self):
        return "<%s Environment>" % self.name

    def has_envvar(self, name):
        return self._userEnvSvc.has(name)
    def get_envvar(self, name, default=None):
        if not self.has_envvar(name):
            return default
        return self._userEnvSvc.get(name)
    def get_all_envvars(self):
        import koprocessutils
        return koprocessutils.getUserEnv()

    def has_pref(self, name):
        ko_name = self._ko_pref_name_from_ci_pref_name.get(name, name)
        for prefset in self.prefsets:
            if prefset.hasPref(ko_name):
                return True
        else:
            return False

    def get_pref(self, name, default=None):
        ko_name = self._ko_pref_name_from_ci_pref_name.get(name, name)
        ko_type = self._ko_pref_type_from_ko_pref_name.get(name, "string")
        for prefset in self.prefsets:
            if prefset.hasPref(ko_name):
                if ko_type == "string":
                    pref = prefset.getStringPref(ko_name)
                elif ko_type == "long":
                    pref = prefset.getLongPref(ko_name)
                elif ko_type == "bool":
                    pref = prefset.getBooleanPref(ko_name)
                else:
                    raise CodeIntelError("unknown Komodo pref type: %r"
                                         % ko_type)
                if ko_name in self._converter_from_ko_pref_name:
                    pref = self._converter_from_ko_pref_name[ko_name](pref)
                return pref
        else:
            return default

    def get_all_prefs(self, name, default=None):
        ko_name = self._ko_pref_name_from_ci_pref_name.get(name, name)
        prefs = []
        for prefset in self.prefsets:
            pref = default
            if prefset.hasPref(ko_name):
                pref = prefset.getStringPref(ko_name)
                if ko_name in self._converter_from_ko_pref_name:
                    pref = self._converter_from_ko_pref_name[ko_name](pref)
            prefs.append(pref)
        return prefs

    def add_pref_observer(self, name, callback):
        """Add a callback for when the named pref changes.

        Note that this can be called multiple times for the same name
        and callback without having to worry about duplicates.
        """
        if name not in self._pref_observer_callbacks_from_name:
            log.debug("%s: start observing '%s' pref", self, name)
            for prefset in self.prefsets:
                prefset.prefObserverService.addObserver(
                    self._wrapped_self, name, 0)
                
            self._pref_observer_callbacks_from_name[name] = {}
        
        self._pref_observer_callbacks_from_name[name][id(callback)] = callback

    def remove_pref_observer(self, name, callback):
        try:
            del self._pref_observer_callbacks_from_name[name][id(callback)]
        except KeyError:
            pass
        if not self._pref_observer_callbacks_from_name[name]:
            log.debug("%s: stop observing '%s' pref", self, name)
            for prefset in self.prefsets:
                prefset.prefObserverService.removeObserver(
                    self._wrapped_self, name)
            del self._pref_observer_callbacks_from_name[name]

    def _notify_pref_observers(self, name):
        if name not in self._pref_observer_callbacks_from_name:
            log.warn("observed '%s' pref change without a callback "
                     "for it: this is unexpected", name)
            return
        callbacks = self._pref_observer_callbacks_from_name[name].values()
        for callback in callbacks:
            try:
                callback(self, name)
            except:
                log.exception("error in pref observer for pref '%s' change",
                              name)

    def observe(self, subject, ko_pref_name, data):
        name = self._ci_pref_name_from_ko_pref_name.get(
                    ko_pref_name, ko_pref_name)
        log.debug("%r: observe '%s' pref change (ko_pref_name=%r)",
                  self, name, ko_pref_name)
        self._notify_pref_observers(name)

    def assoc_patterns_from_lang(self, lang):
        return self._langRegSvc.patternsFromLanguageName(lang)

    def get_proj_base_dir(self):
        if self._unwrapped_proj_weakref is None:
            return None
        unwrapped_proj = self._unwrapped_proj_weakref()
        if unwrapped_proj is None:
            return None
        proj = WrapObject(unwrapped_proj, components.interfaces.koIProject)
        if proj.prefset.hasPref("import_live") \
           and proj.prefset.getBooleanPref("import_live"):
            base_dir = proj.liveDirectory
        else:
            base_dir = dirname(uriparse.URIToLocalPath(proj.url))
        return base_dir


class KoJavaScriptMacroEnvironment(KoCodeIntelEnvironment):
    """A codeintel runtime Environment class for Komodo JS macros. Basically
    the Komodo JavaScript API catalog should always be selected.
    """
    def __init__(self):
        KoCodeIntelEnvironment.__init__(self)
        self.name = "JavaScript Macro"
    def get_pref(self, name, default=None):
        if name != "codeintel_selected_catalogs":
            return KoCodeIntelEnvironment.get_pref(self, name, default)

        value = KoCodeIntelEnvironment.get_pref(self, name, default)
        if value is None:
            value = []
        value.append("komodo")
        return value

class KoPythonMacroEnvironment(KoCodeIntelEnvironment):
    """A codeintel runtime Environment class for Komodo Python macros.
    Basically the Komodo Python libs are added to the extra dirs.
    """
    def __init__(self):
        KoCodeIntelEnvironment.__init__(self)
        self.name = "Python Macro"

    _komodo_python_lib_dir_cache = None
    @property
    def komodo_python_lib_dir(self):
        if self._komodo_python_lib_dir_cache is None:
            koDirSvc = components.classes["@activestate.com/koDirs;1"].\
                getService(components.interfaces.koIDirs)
            self._komodo_python_lib_dir_cache \
                = os.path.join(koDirSvc.mozBinDir, "python")
        return self._komodo_python_lib_dir_cache

    def get_all_prefs(self, name, default=None):
        if name != "pythonExtraPaths":
            return KoCodeIntelEnvironment.get_all_prefs(self, name, default)

        value = KoCodeIntelEnvironment.get_all_prefs(self, name, default)
        if value is None:
            value = []
        value.append(self.komodo_python_lib_dir)
        return value


class KoCodeIntelManager(Manager):
    """Subclass the Manager class:
    - to notify relevant parts of the Komodo UI when a certain scan requests
      complete
    - to add smarts to determine the current scope more efficiently
      (hopefully) -- by caching CIDB data on the current file -- and more
      correctly -- given recent edits and language-specific smarts.
    """
    def __init__(self, db_base_dir=None, extension_pylib_dirs=None,
                 db_event_reporter=None, db_catalog_dirs=None):
        self._phpInfo = components.classes["@activestate.com/koPHPInfoInstance;1"]\
                            .getService(components.interfaces.koIPHPInfoEx)
        Manager.__init__(self, db_base_dir,
                         extra_lang_module_dirs=extension_pylib_dirs,
                         env=KoCodeIntelEnvironment(),
                         db_event_reporter=db_event_reporter,
                         db_catalog_dirs=db_catalog_dirs)
        obsSvc = components.classes["@mozilla.org/observer-service;1"]\
                 .getService(components.interfaces.nsIObserverService)
        self._proxiedObsSvc = getProxyForObject(None,
            components.interfaces.nsIObserverService,
            obsSvc, PROXY_ALWAYS | PROXY_ASYNC)

        # Vars for current scope (CS) smarts.
        self._csLock = threading.RLock()
        self._currFileName = None
        self._currLanguage = None
        #self._flushCSCache()
        self._batchUpdateProgressUIHandler = None

    def set_lang_info(self, lang, silvercity_lexer=None, buf_class=None,
                      import_handler_class=None, cile_driver_class=None,
                      is_cpln_lang=False, langintel_class=None):
        """Override some specific lang handling for Komodo.
        
        Currently just need to tweak some of the import handlers.
        """
        Manager.set_lang_info(self, lang, silvercity_lexer, buf_class,
                              import_handler_class, cile_driver_class,
                              is_cpln_lang,
                              langintel_class=langintel_class)
        
        if lang not in ("Python", "PHP", "Perl", "Tcl", "Ruby"):
            return

        #TODO: drop all this. Should be handled by Environment classes now.

        import_handler = self.citadel.import_handler_from_lang(lang)
        if lang == "Python":
            # Set the "environment path" using the _user's_ Python
            # environment settings, because Komodo messes with that
            # environment.
            import koprocessutils
            userenv = koprocessutils.getUserEnv()
            PYTHONPATH = userenv.get(import_handler.PATH_ENV_VAR, "")
            import_handler.setEnvPath(PYTHONPATH)
        elif lang == "PHP":
            # Getting the PHP include_path is quite difficult and the
            # codeintel package has not yet learned how to do it.
            # Override the PHPImportHandler's .setCorePath()
            # with one smart enough to do it.
            _phpInfo = self._phpInfo
            def _setPHPIncludePath(self, compiler=None, extra=None):
                if compiler:
                    _phpInfo.executablePath = compiler
                if extra:
                    _phpInfo.cfg_file_path = extra
                self.corePath = _phpInfo.include_path.split(os.pathsep)
            import_handler.__class__.setCorePath = _setPHPIncludePath

        # Add the "Additional Perl/Python/Tcl Import Directories" (in that
        # language's preferences panel) to the import path.
        extra_paths_pref_from_lang = {
            "Python": "pythonExtraPaths",
            "Perl": "perlExtraPaths",
            "Tcl": "tclExtraPaths",
            "Ruby": "rubyExtraPaths",
        }
        if lang in extra_paths_pref_from_lang:
            extra_paths_pref = extra_paths_pref_from_lang[lang]
            prefSvc = components.classes["@activestate.com/koPrefService;1"]\
                                .getService().prefs # global prefs
            if prefSvc.hasStringPref(extra_paths_pref):
                extra_paths = prefSvc.getStringPref(extra_paths_pref) \
                                .strip().split(os.pathsep)
                if extra_paths:
                    import_handler.setCustomPath(extra_paths)


#XXX disabled
#    #---- Current Scope (CS) smarts handling.
#    def _flushCSCache(self):
#        self._csLock.acquire()
#        try:
#            self._symbolRows = self._moduleRows = None
#            self._edits = []
#        finally:
#            self._csLock.release()
#
#    def _fillCSCache(self):
#        cx = self.citadel.get_cidb_connection()
#        cu = cx.cursor()
#        self.citadel.acquire_cidb_read_lock()
#        try:
#            cpath = canonicalizePath(self._currFileName)
#            cu.execute("SELECT file.id FROM file, language "
#                       "WHERE language.name=? "
#                       "  AND language.id=file.language_id "
#                       "  AND compare_path=? LIMIT 1",
#                       (self._currLanguage, cpath))
#            for row in cu:
#                file_id = row[0]
#                break
#            else:
#                self._symbolRows = []
#                self._moduleRows = []
#                return
#            cu.execute("SELECT * FROM symbol WHERE file_id=? "
#                       "AND type IN (?, ?, ?) ORDER BY line",
#                       (file_id, ST_CLASS, ST_FUNCTION, ST_INTERFACE))
#            self._symbolRows = tuple(cu)
#            cu.execute("SELECT * FROM module WHERE file_id=%d ORDER BY line" % file_id)
#            self._moduleRows = tuple(cu)
#        finally:
#            self.citadel.release_cidb_read_lock()
#            cu.finalize()
#            cx.close()
#
#    def setCurrentFile(self, filename, language):
#        #XXX Better name for this to make clear that it is just for
#        #    quick curr scope handling for *Citadel* langs
#        self._csLock.acquire()
#        try:
#            self._currLanguage = language
#            if self._currFileName != filename:
#                self._currFileName = filename
#                self._flushCSCache()
#        finally:
#            self._csLock.release()
#
#    def editedCurrentFile(self, scimoz, linesAdded):
#        """Called to notify of line add/remove changes in the curr file."""
#        self._csLock.acquire()
#        try:
#            currLine = scimoz.lineFromPosition(scimoz.currentPos)+1
#            self._edits.insert(0, (currLine+linesAdded, -linesAdded))
#            #XXX This could get *very* large if "enable re-scanning of the
#            #    current file while editing" is disabled. How to handle?
#            #    Does a scan get issued when switching away and back to the
#            #    file?
#        finally:
#            self._csLock.release()

    def _getScopeLine(self, scimoz, position):
        currLine = scimoz.lineFromPosition(position)+1 # 1-based
        scopeLine = currLine
        if self._currLanguage == "Python":
            # The edits-tracking mechanism alone probably doesn't work
            # that well for Python because one can add content to a scope
            # starting from outside that scope's last-scanned
            # line:lineend by just tabing to the appropriate indent
            # level. Instead of applying edits to the current line we
            # look backwards for the appropriate def/class declaration
            # and start on that line.
            nChars, lineContent = scimoz.getLine(currLine-1)
            if re.search("^\s*(def|class) \w+", lineContent):
                # If on "def foo/class Foo" line of the scope, don't move up
                # to the containing scope even though, technically, this
                # is the correct scope for evaluation because:
                # - it doesn't hurt that much: the parent scope will be
                #   searched when a CITDL expression misses in the child
                # - showing the parent scope in the statusbar might
                #   confuse the user
                # - it will allow for the correct scope for one-liners, e.g:
                #       def foo(bar): return bar.spam
                pass
            else:
                indent = ""
                for ch in lineContent:
                    if ch in ' \t':
                        indent += ch
                    else:
                        break
                if ' ' in indent and '\t' in indent:
                    # Do the best we can with mixed tabs and spaces, but
                    # don't worry too much because this is poor form anyway.
                    pattern = r"^(\s{0,%d})(def|class)" % (len(indent)-1)
                elif '\t' in indent:
                    pattern = r"^(\t{0,%d})(def|class)" % (len(indent)-1)
                elif ' ' in indent:
                    pattern = r"^( {0,%d})(def|class)" % (len(indent)-1)
                else: # indent == ''
                    pattern = None
                    scopeLine = 1 # module-level scope
                if pattern:
                    import findlib
                    result = findlib.find(scimoz.text, pattern, position,
                                          patternType="regex-python",
                                          searchBackward=1)
                    if result:
                        scopeLine = scimoz.lineFromPosition(result.start)+1
                        #print "getAdjustedScope: found Python '%s' def'n on "\
                        #      "line %d" % (result.value, scopeLine)

        # The edits-tracking mechanism alone should work well (dogfooding
        # will tell) for brace-languages because one has to add a newline
        # from within the line:lineend range of an existing method to add
        # new content in the scope.
        #XXX If self._edits is long (pick some threshold) then fold those
        #    into cache. Threshold: perhaps a good value is when the
        #    number of edits exceeds half the number of scope elements.
        if len(self._edits) > 50:
            log.warn("Currently managing a large number of line edits (%d) "
                     "in 'current scope handling'. Should consider "
                     "refreshing status." % len(self._edits))
        #print "getAdjustedScope: apply edits: %r" % self._edits
        for line, linesAdded in self._edits:
            if line <= scopeLine:
                scopeLine += linesAdded
        #print "getAdjustedScope: scope line %d -> %d" % (currLine, scopeLine)
        return scopeLine

    def _getScopeFromCache(self, scopeLine):
        XXX
        symbolRow = None
        for row in self._symbolRows:
            if (row[S_LINE] <= scopeLine and
                (row[S_LINEEND] is None or scopeLine <= row[S_LINEEND])):
                symbolRow = row
            elif row[S_LINE] > scopeLine:
                break # we've already gone past the given "scopeLine"
        if symbolRow:
            return (symbolRow[S_FILE_ID], "symbol", symbolRow[S_ID], symbolRow)

        if not self._moduleRows:
            raise CodeIntelError("there are no CIDB module entries for "
                                 "file '%s'" % self._currFileName)
        moduleRow = None
        for row in self._moduleRows:
            if row[M_LINE] <= scopeLine:
                moduleRow = row
            elif row[M_LINE] > scopeLine:
                break # we've gone past the given "scopeLine"
        if not moduleRow:
            raise CodeIntelError("unexpectedly, no module rows for file "
                                 "'%s' correspond to scopeLine %d"
                                 % (self._currFileName, scopeLine))
        return (moduleRow[M_FILE_ID], "module", moduleRow[M_ID], moduleRow)

    def getAdjustedCurrentScope(self, scimoz, position):
        """A getScopeForFileAndLine() adjusted for recent edits."""
        XXX
        self._csLock.acquire()
        try:
            scopeLine = self._getScopeLine(scimoz, position)
            try:
                if self._moduleRows is None: self._fillCSCache()
                retval = self._getScopeFromCache(scopeLine)
            except (CodeIntelError, ValueError), ex:
                # Note: Silently ignore errors from this. CodeIntelError
                # mostly just mean that the file was just opened and the
                # statusbar wants to know the current scope. We are probably
                # at the first line, so the default is fine. Database errors
                # may include:
                #   OperationalError: database is locked
                log.debug(str(ex))
                retval = (0, None, 0, None)
            return retval
        finally:
            self._csLock.release()

    def getAdjustedCurrentScopeInfo(self, scimoz, position):
        """A getScopeInfoForFileAndLine() adjusted for recent edits."""
        XXX
        self._csLock.acquire()
        try:
            file_id, table, id, row = self.getAdjustedCurrentScope(scimoz, position)
            if table is None:
                retval = (None, None, None, None)
            elif table == "module":
                retval = ("module", row[M_NAME], None, None)
            else:
                type, id, scope, name, attrStr =\
                    row[S_TYPE], row[S_ID], row[S_SCOPE], row[S_NAME], row[S_ATTRIBUTES]
                attributes = parseAttributes(attrStr)
                typeName = symbolType2Name(type)
                imageURL = cb.getImageURLForSymbol(typeName, attributes)
                desc = cb.getDescForSymbol(typeName, name, attributes, scope,
                                           self._currLanguage)
                retval = (typeName, name, imageURL, desc)
            return retval
        finally:
            self._csLock.release()


class KoCodeIntelEvalController(EvalController):
    _com_interfaces_ = [components.interfaces.koICodeIntelEvalController]
    _reg_clsid_ = "{020FE3F2-BDFD-4F45-8F13-D70A1D6F4D82}"
    _reg_contractid_ = "@activestate.com/koCodeIntelEvalController;1"
    _reg_desc_ = "Komodo CodeIntel Evaluation Controller"
    
    log = None
    have_errors = have_warnings = False
    got_results = False
    ui_handler = None
    ui_handler_proxy_sync = None

    def debug(self, msg, *args):
        if self.log is None: self.log = []
        self.log.append(("debug", msg, args))
    def info(self, msg, *args):
        if self.log is None: self.log = []
        self.log.append(("info", msg, args))
    def warn(self, msg, *args):
        if self.log is None: self.log = []
        self.log.append(("warn", msg, args))
        self.have_warnings = True
    def error(self, msg, *args):
        if self.log is None: self.log = []
        self.log.append(("error", msg, args))
        self.have_errors = True

    def set_ui_handler(self, ui_handler):
        self.ui_handler = ui_handler
        # Make a synchronous proxy for sending back CI info. The setXXX
        # functions in the UI wrap themselves in a setTimeout() call to
        # avoid delaying this codepath. Calling setDefinitionsInfo
        # asynchronously caused hard crash, see bug:
        # http://bugs.activestate.com/show_bug.cgi?id=65188
        self.ui_handler_proxy_sync = getProxyForObject(None,
            components.interfaces.koICodeIntelCompletionUIHandler,
            self.ui_handler, PROXY_ALWAYS | PROXY_SYNC)

    # ACIID == AutoComplete Image ID
    koICodeIntelCompletionUIHandler \
        = components.interfaces.koICodeIntelCompletionUIHandler
    aciid_from_type = {
        "class":    koICodeIntelCompletionUIHandler.ACIID_CLASS,
        "function": koICodeIntelCompletionUIHandler.ACIID_FUNCTION,
        "module":   koICodeIntelCompletionUIHandler.ACIID_MODULE,
        "interface": koICodeIntelCompletionUIHandler.ACIID_INTERFACE,
        "namespace": koICodeIntelCompletionUIHandler.ACIID_NAMESPACE,
        "variable": koICodeIntelCompletionUIHandler.ACIID_VARIABLE,
        "$variable": koICodeIntelCompletionUIHandler.ACIID_VARIABLE_SCALAR,
        "@variable": koICodeIntelCompletionUIHandler.ACIID_VARIABLE_ARRAY,
        "%variable": koICodeIntelCompletionUIHandler.ACIID_VARIABLE_HASH,
        "directory": koICodeIntelCompletionUIHandler.ACIID_DIRECTORY,

        "element": koICodeIntelCompletionUIHandler.ACIID_XML_ELEMENT,
        "attribute": koICodeIntelCompletionUIHandler.ACIID_XML_ATTRIBUTE,
        
        # Added for CSS, may want to have a better name/images though...
        "value": koICodeIntelCompletionUIHandler.ACIID_VARIABLE,
        "property": koICodeIntelCompletionUIHandler.ACIID_CLASS,
        "pseudo-class": koICodeIntelCompletionUIHandler.ACIID_INTERFACE,
        "rule": koICodeIntelCompletionUIHandler.ACIID_FUNCTION,

        #TODO: add the following (for CSS, XML/HTML, etc.)
        # "comment"? (in use)
        # "doctype"? (in use)
        # "namespace"? (in use)
        # "cdata"? (in use)
        # "attribute_value"? (in use, I'd prefer the use of a hyphen)

        # Handle fallbacks? "ACIID_VARIABLE" for Ruby. That should be done
        # in post-processing.
    }
    
    def cplns_with_aciids_from_cplns(self, cplns):
        """Translate a list of completion tuples
            [(<type>, <value>), ...]
        into a list of completions with image references as Scintilla
        wants them
            ["<value>?1", ...]
        
        See the CodeIntelCompletionUIHandler ctor in codeintel.js for
        registration of the image XPMs.
        """
        aciid_from_type = self.aciid_from_type
        cplns_with_aciids = []
        for t, v in cplns:
            try:
                cplns_with_aciids.append("%s?%d" % (v, aciid_from_type[t]))
            except KeyError:
                cplns_with_aciids.append(v)
        return cplns_with_aciids

    def set_cplns(self, cplns):
        self.got_results = True
        cplns_with_aciids = self.cplns_with_aciids_from_cplns(cplns)
        cplns_str = self.buf.scintilla_cpln_sep.join(cplns_with_aciids)
        #XXX Might want to include relevant string info leading up to
        #    the trigger char so the Completion Stack can decide
        #    whether the completion info is still relevant.
        self.ui_handler_proxy_sync.setAutoCompleteInfo(cplns_str, self.trg.pos)

    def set_calltips(self, calltips):
        self.got_results = True
        calltip = calltips[0]
        self.ui_handler_proxy_sync.setCallTipInfo(calltip, self.trg.pos,
                                                  not self.trg.implicit)

    def set_defns(self, defns):
        self.got_results = True
        self._last_defns = defns
        self.ui_handler_proxy_sync.setDefinitionsInfo(defns, self.trg.pos)

    def done(self, reason):
        # This part of the spec describes what the IDE user UI should be
        # on autocomplete/calltips:
        #   http://specs.tl.activestate.com/kd/kd-0100.html#k4-completion-ui-notes
        # Currently 'reason' isn't a reliable mechanism for determining
        # state.
        if self.got_results:
            #XXX What about showing warnings even if got results?
            pass # success: show the completions, already done
        elif self.is_aborted():
            pass # aborted: we've moved on to another completion
        else:
            # We'll show a statusbar message -- highlighted if the trigger
            # was explicit (Ctrl+J). The message will mention warnings
            # and errors, if any. If explicit the whole controller log is
            # dumped to Komodo's log for possible bug reporting.
            desc = self.desc
            if not desc:
                desc = {TRG_FORM_CPLN: "completions",
                        TRG_FORM_CALLTIP: "calltip",
                        TRG_FORM_DEFN: "definition"}.get(self.trg.form, "???")
            if self.have_errors:
                # ERRORS... (error(s) determining completions)
                msg = '; '.join((m % args) for lvl,m,args in self.log
                                if lvl == "error")
                msg += " (error determining %s)" % desc
                log.error("error evaluating %s:\n  trigger: %s\n  log:\n%s",
                          desc, self.trg,
                          indent('\n'.join("%s: %s" % (lvl, m%args)
                                           for lvl,m,args in self.log)))
            else:
                # No calltip|completions found (WARNINGS...)
                msg = "No %s found" % desc
                if self.have_warnings:
                    warns = ', '.join((("warning: "+m) % args)
                                      for lvl,m,args in self.log
                                      if lvl == "warn")
                    msg += " (%s)" % warns
            self.ui_handler_proxy_sync.setStatusMessage(
                msg, not self.trg.implicit)

        EvalController.done(self, reason)
        #XXX Should we clean up the UI handler and proxy? Are we leaking?
        #    And the obs svc, too?


class KoCodeIntelBatchUpdater(BatchUpdater):
    """An adaptor between the codeintel BatchUpdater API and
    koICodeIntelBatchUpdateProgressUIHandler that drives Komodo's "Build
    CIDB" wizard.
    """
    _com_interfaces_ = [components.interfaces.koICodeIntelBatchUpdater]
    _reg_clsid_ = "{40F1F58F-D81B-44B9-B71B-2C89A39F63EC}"
    _reg_contractid_ = "@activestate.com/koCodeIntelBatchUpdater;1"
    _reg_desc_ = "Komodo CodeIntel Batch Updater"

    def __init__(self):
        BatchUpdater.__init__(self)

        # Save reported errors for reporting to user later.
        # Do we want to save other logging? Warnings?
        self.errors = []
        self.progress_cache = {}
        self.progress_ui_handler = None
        self.progress_ui_handler_proxy = None

    def set_progress_ui_handler(self, progress_ui_handler):
        self.progress_ui_handler = progress_ui_handler
        self.progress_ui_handler_proxy = getProxyForObject(None,
            components.interfaces.koICodeIntelBatchUpdateProgressUIHandler,
            self.progress_ui_handler, PROXY_ALWAYS | PROXY_ASYNC)
        
    def error(self, msg, *args):
        self.errors.append((msg, args))
    def get_error_log(self):
        return [(msg % args) for msg,args in self.errors]

    def done(self, reason):
        ui = self.progress_ui_handler_proxy
        if not ui:
            return

        if self.is_aborted():
            ui.cancelled()
        elif self.errors:
            errors_str = '\n'.join((msg%args) for msg,args in self.errors)
            ui.erroredout(errors_str)
        else:
            ui.completed()

        BatchUpdater.done(self, reason)

    def progress(self, stage, obj):
        ui = self.progress_ui_handler_proxy
        if not ui:
            return
        
        # With an "upgrade" batch update we expect progress notifications
        # of this form:
        #   "upgrade", (<current-stage-message>, <percentage-done>)
        if stage == "upgrade":
            stage_msg, percent = obj
            ui.setStatusMessage("Upgrade: "+stage_msg)
            ui.setProgressMeterValue(percent)
            return
        
        # With other batch updates progress notifications are of these forms:
        #   "importing", <path of CIX file being imported>
        #   "gathering files", <number of files found>
        #   "scanning", <ScanRequest object>
        #   <some string>, <some object>      # should be graceful
        remaining = eta = percent = None
        percent_from_importing = self.progress_cache.setdefault(
            "percent_from_importing", 0.00)
        percent_from_gathering = self.progress_cache.setdefault(
            "percent_from_gathering", 0.00)
        percent_from_scanning = self.progress_cache.setdefault(
            "percent_from_scanning", 0.00)

        if stage == "importing":
            msg = "Importing: %s" % obj
            # just want at least 5% on first CIX file import
            percent_from_importing = 0.05
        elif stage == "gathering files":
            remaining = self.num_files_to_process() + 1
            msg = "Gathering files..."
            times_gathering = self.progress_cache.setdefault("times_gathering", 0)
            times_gathering += 1
            percent_from_gathering = min(0.01*times_gathering, 0.05)
            self.progress_cache["times_gathering"] = times_gathering
        elif stage == "scanning": # 'obj' is a request object
            remaining = self.num_files_to_process() + 1
            total = self.progress_cache.setdefault("total_files", remaining)
            # Progress meter
            percent_from_scanning = float(total-remaining)/float(total)
            # Message
            # 79: display width; 25: other stuff, e.g. "Scanning", "ETA: ..."
            msg = "Scanning: %s" % obj.path
            # ETA
            FEW = 50
            now = time.time()
            try:
                last_few = self.progress_cache["last_few"]
            except KeyError:
                self.progress_cache["last_few"] = [now]
            else:
                secs_for_one = (now - last_few[0]) / len(last_few)
                last_few.append(now)
                if len(last_few) > FEW: del last_few[0]
                self.progress_cache["last_few"] = last_few

                remain_secs = secs_for_one * float(remaining)
                if remain_secs < 60.0:
                    eta = "%d sec" % int(remain_secs)
                elif (remain_secs/60.0 < 60.0):
                    eta = "%d min" % int(remain_secs/60.0)
                else:
                    eta = "%d hr" % int(remain_secs/3600.0)

            # Adjust percent so it ranges over the remainder after the
            # accumulated "Gathering" and "Importing" percentages have been
            # factored out.
            sucked_up = percent_from_importing + percent_from_gathering
            percent_from_scanning = percent_from_scanning * (1.00-sucked_up)
        else:
            if obj:
                msg = "%s: %s" % (stage, obj)
            else:
                msg = stage
        self.progress_cache["percent_from_importing"] = percent_from_importing
        self.progress_cache["percent_from_gathering"] = percent_from_gathering
        self.progress_cache["percent_from_scanning"] = percent_from_scanning
        if percent is None:
            percent = percent_from_importing \
                      + percent_from_gathering \
                      + percent_from_scanning

        ui.setStatusMessage(msg)
        if remaining is None: remaining = -1
        ui.setRemainingFiles(remaining)
        if percent is not None:
            ui.setProgressMeterValue(int(percent*100.0))
        ui.setETA(eta)


class KoCodeIntelDBUpgrader(threading.Thread):
    """Upgrade the DB and show progress."""
    _com_interfaces_ = [components.interfaces.koIShowsProgress]
    _reg_clsid_ = "{911F5139-2648-4FAB-A774-2F8595B3A396}"
    _reg_contractid_ = "@activestate.com/koCodeIntelDBUpgrader;1"
    _reg_desc_ = "Komodo CodeIntel Database Upgrader"

    controller = None
    def set_controller(self, controller):
        self.controller = getProxyForObject(None,
            components.interfaces.koIProgressController,
            controller, PROXY_ALWAYS | PROXY_SYNC)
        self.controller.set_progress_mode("undetermined")
        self.start()
    
    def run(self):
        try:
            ciSvc = components.classes["@activestate.com/koCodeIntelService;1"].\
                       getService(components.interfaces.koICodeIntelService)
            UnwrapObject(ciSvc).upgradeDB()
        except DatabaseError, ex:
            errmsg = ("Could not upgrade your Code Intelligence Database "
                      "because: %s. Your database will be backed up "
                      "and a new empty database will be created." % ex)
            errtext = None
            ciSvc.resetDB();
        except:
            errmsg = ("Unexpected error upgrading your database. "
                      "Your database will be backed up "
                      "and a new empty database will be created.")
            errtext = traceback.format_exc()
            ciSvc.resetDB();
        else:
            errmsg = None
            errtext = None

        prefSvc = components.classes["@activestate.com/koPrefService;1"]\
                    .getService().prefs # global prefs
        prefSvc.setBooleanPref("codeintel_have_preloaded_database", 0)

        self.controller.done(errmsg, errtext)


class KoCodeIntelDBPreloader(threading.Thread):
    _com_interfaces_ = [components.interfaces.koIShowsProgress]
    _reg_clsid_ = "{A456B064-2A30-4F87-8BCF-39F6B19C4D53}"
    _reg_contractid_ = "@activestate.com/koCodeIntelDBPreloader;1"
    _reg_desc_ = "Komodo CodeIntel Database Preloader"

    controller = None
    _progress_mode_cache = None
    cancelling = False
    
    def set_controller(self, controller):
        self.controller = getProxyForObject(None,
            components.interfaces.koIProgressController,
            controller, PROXY_ALWAYS | PROXY_SYNC)
        self.controller.set_progress_mode("undetermined")
        self.start()

    def cancel(self):
        self.cancelling = True

    def run(self):
        errmsg = None
        errtext = None
        try:
            try:
                ciSvc = components.classes["@activestate.com/koCodeIntelService;1"].\
                           getService(components.interfaces.koICodeIntelService)
                mgr = UnwrapObject(ciSvc).mgr
                
                # Stage 1: stdlibs zone
                # For now we preload stdlibs for a hardcoded set of langs.
                # Eventually would want to tie this to answers from a "Komodo
                # Startup Wizard" that would ask the user what languages they
                # use.
                self.controller.set_stage("Preloading standard library data.")
                stdlibs_zone = mgr.db.get_stdlibs_zone()
                if stdlibs_zone.can_preload():
                    stdlibs_zone.preload(self.progress_cb)
                else:
                    self.controller.set_progress_value(0)
                    #self.controller.set_progress_mode("determined")
                    langs = ["JavaScript", "Ruby", "Perl", "PHP", "Python"]
                    value_base = 5
                    value_incr = 95/len(langs)
                    for i, lang in enumerate(langs):
                        if self.cancelling:
                            return
                        self.controller.set_progress_value(value_base)
                        self.value_span = (value_base, value_base+value_incr)
                        stdlibs_zone.update_lang(lang, self.progress_cb)
                        value_base += value_incr
                
                # Stage 2: catalog zone
                # Preload catalogs that are enabled by default (or perhaps
                # more than that.) For now we preload all of them.
                self.controller.set_stage("Preloading catalogs.")
                self.controller.set_progress_value(0)
                self.value_span = (0, 100)
                self.controller.set_desc("")
                self.controller.set_progress_mode("determined")
                catalogs_zone = mgr.db.get_catalogs_zone()
                catalog_selections = mgr.env.get_pref("codeintel_selected_catalogs")
                catalogs_zone.update(catalog_selections,
                                     progress_cb=self.progress_cb)

                prefSvc = components.classes["@activestate.com/koPrefService;1"]\
                            .getService().prefs # global prefs
                prefSvc.setBooleanPref("codeintel_have_preloaded_database", 1)
            except Exception, ex:
                errmsg = "Error preloading DB: %s" % ex
                errtext = traceback.format_exc()
        finally:
            self.controller.done(errmsg, errtext)

    def progress_cb(self, desc, value):
        """Progress callback passed to db .update() methods.
        Scale the given value by `self.value_span'.
        """
        progress_mode = value is None and "undetermined" or "determined"
        if progress_mode != self._progress_mode_cache:
            self.controller.set_progress_mode(progress_mode)
            self._progress_mode_cache = progress_mode
        self.controller.set_desc(desc)
        if progress_mode == "determined":
            lower, upper = self.value_span
            scaled_value = (upper-lower) * value / 100 + lower
            self.controller.set_progress_value(scaled_value)



class KoCodeIntelService:
    _com_interfaces_ = [components.interfaces.koICodeIntelService]
    _reg_clsid_ = "{CF1F65B6-25EC-4FB3-A2CB-241CB436E377}"
    _reg_contractid_ = "@activestate.com/koCodeIntelService;1"
    _reg_desc_ = "Komodo Code Intelligence Service"

    def __init__(self):
        self.isBackEndActive = False
        self._koDirSvc = components.classes["@activestate.com/koDirs;1"].\
                   getService(components.interfaces.koIDirs)

        # Find extensions that may have codeintel lang-support modules.
        extension_pylib_dirs = []
        for ext_dir in directoryServiceUtils.getExtensionDirectories():
            ext_codeintel_dir = join(ext_dir, "pylib")
            if exists(ext_codeintel_dir):
                extension_pylib_dirs.append(ext_codeintel_dir)

        self.mgr = KoCodeIntelManager(
            os.path.join(self._koDirSvc.hostUserDataDir, "codeintel"),
            extension_pylib_dirs=extension_pylib_dirs,
            db_event_reporter=self._reportDBEvent,
            db_catalog_dirs=list(self._genDBCatalogDirs()))

        obsSvc = components.classes["@mozilla.org/observer-service;1"]\
                 .getService(components.interfaces.nsIObserverService)
        self._proxiedObsSvc = getProxyForObject(None,
            components.interfaces.nsIObserverService,
            obsSvc, PROXY_ALWAYS | PROXY_ASYNC)
        self.fileSvc = components.classes["@activestate.com/koFileService;1"]\
            .getService(components.interfaces.koIFileService)
        self.langRegistrySvc = components.classes['@activestate.com/koLanguageRegistryService;1'].\
             getService(components.interfaces.koILanguageRegistryService)
        self.partSvc = components.classes["@activestate.com/koPartService;1"]\
            .getService(components.interfaces.koIPartService)

        #TODO: Currently this never gets cleared.
        self._proj_env_from_proj_id_cache = {}

        # XXX todo, add pref observer to turn on/off showing projects
        self.prefSvc = components.classes["@activestate.com/koPrefService;1"]\
                            .getService().prefs # global prefs
        self._showProjects = self.prefSvc.getBooleanPref('codeintel_browser_show_projects')
        self._showAllProjects = self.prefSvc.getBooleanPref('codeintel_browser_show_all_projects')
        self._projects = []

        #XXX Obsolete. Remove usage of and dependence on this. The
        #    KoCodeBrowserTreeView maintains a list of open buffers that
        #    is authoritative.
        self._files = [] #files that were sent via opened_document

    def _genDBCatalogDirs(self):
        """Yield all possible dirs in which to look for API Catalogs.

        Note: This doesn't filter out non-existant directories.
        """
        yield join(self._koDirSvc.userDataDir, "apicatalogs")    # user
        for extensionDir in directoryServiceUtils.getExtensionDirectories():
            yield join(extensionDir, "apicatalogs")             # user-install exts
        yield join(self._koDirSvc.commonDataDir, "apicatalogs")  # site/common
        # factory: handled by codeintel system (codeintel2/catalogs/...)

    def _reportDBEvent(self, desc):
        self._sendStatusMessage(desc)

    def needToUpgradeDB(self):
        """Return true if the db needs to be upgraded. Raise an
        exception and setLastError() if cannot upgrade or if db looks
        inappropriate.
        """
        try:
            state, details = self.mgr.db.upgrade_info()
        except CodeIntelError, ex:
            msg = "unexpected error getting DB upgrade info (see error log): %s" % ex
            log.exception(msg)
            lastErrorSvc = components.classes["@activestate.com/koLastErrorService;1"]\
                           .getService(components.interfaces.koILastErrorService)
            lastErrorSvc.setLastError(0, msg)
            raise ServerException(nsError.NS_ERROR_FAILURE, msg)
        if state == Database.UPGRADE_NOT_NECESSARY:
            return False
        elif state == Database.UPGRADE_NECESSARY:
            return True
        elif state == Database.UPGRADE_NOT_POSSIBLE:
            lastErrorSvc = components.classes["@activestate.com/koLastErrorService;1"]\
                           .getService(components.interfaces.koILastErrorService)
            lastErrorSvc.setLastError(0, details)
            raise ServerException(nsError.NS_ERROR_FAILURE, details)

    def resetDB(self):
        self.mgr.db.reset(backup=True)

    def upgradeDB(self):
        self.mgr.db.upgrade()

    def activateBackEnd(self):
        if self.isBackEndActive: return
        try:
            self.mgr.initialize()
        except (CodeIntelError, EnvironmentError), ex:
            err = "Error activating Code Intelligence backend: "+str(ex)
            lastErrorSvc = components.classes["@activestate.com/koLastErrorService;1"]\
                           .getService(components.interfaces.koILastErrorService)
            lastErrorSvc.setLastError(0, err)
            raise ServerException(nsError.NS_ERROR_FAILURE, err)
        else:
            self.isBackEndActive = True

    def deactivate(self):
        #XXX Is there a problem with ref cycle btwn codeBrowserMgr and mgr?
        #XXX Can the codeintel system survice a deactivate/re-activate cycle?
        self.isBackEndActive = False
        self.mgr.finalize()

    def _getCIPath(self, document):
        if document.isUntitled:
            cipath = os.path.join("<Unsaved>", document.displayPath)
        else:
            cipath = document.displayPath
        return cipath

    def ideEvent_EditedCurrentDocument(self, document, scimoz, linesAdded,
                                       rescan=False):
        lang = document.language
        # XXX FIXME post beta 1
        #if self.mgr.is_xml_lang(lang):
        #    if rescan:
        #        request = XMLParseRequest(document.ciBuf, PRIORITY_CURRENT)
        #        self.mgr.idxr.stage_request(request, 0.5)
        if self.mgr.is_citadel_lang(lang):
            if rescan:
                if linesAdded:
                    request = ScanRequest(document.ciBuf, PRIORITY_IMMEDIATE)
                    self.mgr.idxr.stage_request(request, 0)
                else:
                    request = ScanRequest(document.ciBuf, PRIORITY_CURRENT)
                    self.mgr.idxr.stage_request(request, 1.5)

    def _addURIRequest(self, uri):
        XXX
        # Determine the probable language from the file basename.
        f = self.fileSvc.getFileFromURI(uri)
        lang = self.langRegistrySvc.suggestLanguageForFile(f.baseName)
        cipath = f.displayPath
        mtime = f.lastModifiedTime
        return self._addPathRequest(cipath, lang, mtime=mtime)

    def _addPathRequest(self, cipath, lang, name="opened_document",
                        document=None, mtime=None):
        XXX
        if self.mgr.is_citadel_lang(lang):
            if name == "opened_document":
                #self.mgr.setCurrentFile(cipath, lang)
                if document:
                    mtime = document.file and document.file.lastModifiedTime or None
            request = ScanRequest(buf, PRIORITY_CURRENT, mtime=mtime)
            self.mgr.idxr.stage_request(request)
            return 1
        return 0

    def ideEvent(self, name, sdata, odata):
        # XXX PERF Note:
        # - Project support + live projects have not been investigated.
        #   This results in LOTS of IDE events being sent (and unnecessarily
        #   for the common case: that pref is turned off).
        try:
            if name in ("opened_document", "changed_document"):
                url, document = sdata, odata
                language = document.language
                log.info("ideEvent %s: %s (%s)", name, document.displayPath, language)
                if self.mgr.is_citadel_lang(language):
                    mtime = document.file and document.file.lastModifiedTime or None
                    buf = UnwrapObject(document.ciBuf)
                    if buf is not None:
                        # sometimes occurs, race condition in opening?
                        request = ScanRequest(buf, PRIORITY_OPEN, mtime=mtime)
                        self.mgr.idxr.stage_request(request)
            elif name == "closing_document":
                dummy, document = sdata, odata
                if self._showProjects and not document.isUntitled:
                    for p in self._projects:
                        urls = p.getAllContainedURLs()
                        if document.file.URI in urls:
                            return
            #XXX TODO for project files in Code Browser:
            # - uriparse.displayPath() is what we want
            # - need to have added to and removed from project in
            #   added_file_to_project and removed_file_from_project events
            # - figure out a W.S. system for these that is efficient
            # - get a separate mode for PseudoModule in cb.py (perhaps
            #   should finally separate PseudoModule into parts: three
            #   sub-classes: ErrorNode, NotScannedModule, ScanningModule)
            # - consider adding a toggle button to turn off "files in
            #   projects"
            # - consider adding a "Scan" entry to context menu on modules
            #   in C.B.
            elif name == "current_project_changed":
                if self._showAllProjects or not self._showProjects:
                    return
                XXX
                dummy, project = sdata, odata.QueryInterface(components.interfaces.koIProject)
                if self._projects and project == self._projects[0]:
                    return
                if self._projects:
                    urls = self._projects[0].getAllContainedURLs()
                    for url in urls:
                        f = self.fileSvc.getFileFromURI(url)
                        cipath = f.displayPath
                    self._projects = []
                if project:
                    self._projects.append(project)
                    urls = self._projects[0].getAllContainedURLs()
                    for url in urls:
                        self._addURIRequest(url)
            elif name == "closing_project":
                # remove all project files that are not open in the editor
                if not self._showProjects or not self._projects:
                    return
                XXX
                dummy, project = sdata, odata.QueryInterface(components.interfaces.koIProject)
                if not self._showAllProjects and project != self._projects[0]:
                    return
                urls = project.getAllContainedURLs()
                self._projects.remove(project)
                for url in urls:
                    f = self.fileSvc.getFileFromURI(url)
            #    print "XXX:TODO let CodeBrowserMgr know project '%s' is closing" % project.name
            elif name == "opened_project":
                if not self._showProjects or (not self._showAllProjects and self._projects):
                    return
                XXX
                # add all project files that are in the project
                url, project = sdata, odata.QueryInterface(components.interfaces.koIProject)
                self._projects.append(project)
                log.info("ideEvent %s: project='%s'", project.name, os.path.basename(project.url))
                urls = project.getAllContainedURLs()
                for url in urls:
                    self._addURIRequest(url)
            #    pprint(urls)
            elif name == "added_file_to_project":
                if not self._showProjects:
                    return
                XXX
                url, project = sdata, odata.QueryInterface(components.interfaces.koIProject)
                log.info("ideEvent %s: %s", name, os.path.basename(url))
                self._addURIRequest(url)
            elif name == "removed_file_from_project":
                if not self._showProjects:
                    return
                XXX
                url, project = sdata, odata.QueryInterface(components.interfaces.koIProject)
                log.info("ideEvent %s: %s", name, os.path.basename(url))
                for p in self._projects:
                    urls = p.getAllContainedURLs()
                    if url in urls:
                        return
                f = self.fileSvc.getFileFromURI(url)
            elif (name == "switched_current_language" or
                  name == "switched_current_document"):
                # Switched current editor pane view to a new document or
                # changed that document's language.
                dummy, document = sdata, odata

                #TODO Used to kick off a ScanRequest for this new
                #     current document. I don't think that is
                #     necessary. If that is the case, then remove
                #     the "switched_current_document" ideEvent.
                log.info("ideEvent %s: %s (%s)", name,
                         document.displayPath, document.language)
            else:
                raise NotImplementedError("unknown ideEvent name: '%s'" % name)
        except Exception, ex:
            #XXX Error trapping. Should just log and then drop any error... i.e.
            #    I don't think there is anything here that should be fatal.
            log.exception("Whoa! Unexpected failure handling ideEvent '%s'"
                          % name)

    def batch_update(self, join, updater):
        # Unwrap controller so the non-IDL-ified attributes can be used by
        # the guts of the codeintel system.
        ##self.__cached_real_ctlr = ctlr #XXX weakref? necessary?
        ##self.mgr.batch_update_start(wait=wait, ctlr=UnwrapObject(ctlr))
        
        #XXX Should we unwrap the updater obj?
        unwrapped_updater = UnwrapObject(updater)
        self.mgr.batch_update(join=join, updater=unwrapped_updater)

    def _proj_env_from_koIDocument(self, doc):
        """Return an Environment instance appropriate for the given
        koIDocument. If this doc is part of an open Komodo project then
        the Environment instance will wrap that project's prefs.

        Returns None, if this document is not part of a project.
        """
        if doc.file and doc.file.URI:
            proj = self.partSvc.getProjectForURL(doc.file.URI)
            if proj:
                if proj.id not in self._proj_env_from_proj_id_cache:
                    self._proj_env_from_proj_id_cache[proj.id] \
                        = KoCodeIntelEnvironment(proj)
                proj_env = self._proj_env_from_proj_id_cache[proj.id]
                return proj_env
        return None

    def buf_from_koIDocument(self, doc, prefset=None):
        path = doc.displayPath
        if path.startswith("macro://"):
            # Ensure macros get completion for the relevant Komodo APIs.
            if path.endswith(".js"):
                env = KoJavaScriptMacroEnvironment()
            elif path.endswith(".py"):
                env = KoPythonMacroEnvironment()
            else:
                log.warn("unexpected 'macro://' document that doesn't end "
                         "with '.js' or '.py': '%s'", path)
                env = None
        else:
            # If this document is part of an open project, hook up that
            # project's prefset.
            env = self._proj_env_from_koIDocument(doc)
        return self.mgr.buf_from_koIDocument(doc, env=env)

    def is_registered_lang(self, lang):
        return self.mgr.is_registered_lang(lang)
    def is_cpln_lang(self, lang):
        return self.mgr.is_cpln_lang(lang)
    def get_cpln_langs(self):
        return self.mgr.get_cpln_langs()
    def is_citadel_lang(self, lang):
        return self.mgr.is_citadel_lang(lang)
    def get_citadel_langs(self):
        return self.mgr.get_citadel_langs()
    def is_xml_lang(self, lang):
        return self.mgr.is_xml_lang(lang)

    def getScopeForFileAndLine(self, path, line):
        return self.mgr.getScopeForFileAndLine(path, line)[:3]
    def getScopeInfoForFileAndLine(self, path, line, language):
        return self.mgr.getScopeInfoForFileAndLine(path, line, language)
    def getAdjustedCurrentScope(self, scimoz, position):
        return self.mgr.getAdjustedCurrentScope(scimoz, position)[:3]
    def getAdjustedCurrentScopeInfo(self, scimoz, position):
        return self.mgr.getAdjustedCurrentScopeInfo(scimoz, position)

    def _sendStatusMessage(self, msg, highlight=0, timeout=5000):
        sm = components.classes["@activestate.com/koStatusMessage;1"]\
             .createInstance(components.interfaces.koIStatusMessage)
        sm.category = "codeintel"
        sm.msg = msg
        sm.timeout = timeout
        sm.highlight = highlight
        try:
            self._proxiedObsSvc.notifyObservers(sm, "status_message", None)
        except COMException, ex:
            pass

#XXX Obsolete
#    def getMembers(self, language, path, line, citdl, explicit,
#                   scopeFileId=0, scopeTable=None, scopeId=0, content=None):
#        self._sendStatusMessage("XXX: NYI: getMembers", explicit)
#        return [], []
#
#        types, members = [], []
#        try:
#            typesAndMembers = self.manager.getMembers(language, path, line,
#                citdl, scopeFileId, scopeTable, scopeId, content)
#            if not typesAndMembers:
#                self._sendStatusMessage(
#                    "No AutoComplete members for '%s'" % citdl, explicit)
#            else:
#                for t,m in typesAndMembers:
#                    types.append(t)
#                    members.append(m)
#        except CodeIntelError, ex:
#            self._sendStatusMessage("error determining members: "+str(ex),
#                                    explicit)
#        return types, members
#
#    def getCallTips(self, language, path, line, citdl, explicit,
#                    scopeFileId=0, scopeTable=None, scopeId=0, content=None):
#        self._sendStatusMessage("XXX: NYI: getCallTips", explicit)
#        return []
#
#        try:
#            calltips = self.manager.getCallTips(language, path, line, citdl,
#                scopeFileId, scopeTable, scopeId, content)
#            if not calltips:
#                self._sendStatusMessage("No CallTip for '%s'" % citdl,
#                                        explicit)
#        except CodeIntelError, ex:
#            self._sendStatusMessage("error determining CallTip: "+str(ex),
#                                    explicit)
#            calltips = []
#        return calltips
#
#    def getSubimports(self, language, module, cwd, explicit):
#        self._sendStatusMessage("XXX: NYI: getSubimports", explicit)
#        return []
#
#        try:
#            subimports = self.manager.getSubimports(language, module, cwd,
#                                                    explicit)
#            if not subimports:
#                self._sendStatusMessage("No available subimports for '%s'" % module,
#                                        explicit)
#        except CodeIntelError, ex:
#            self._sendStatusMessage("error determining subimports: "+str(ex),
#                                    explicit)
#            subimports = []
#        return subimports

