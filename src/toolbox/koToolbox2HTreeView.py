# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 1.1/GPL 2.0/LGPL 2.1
# 
# The contents of this file are subject to the Mozilla Public License
# Version 1.1 (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://www.mozilla.org/MPL/
# 
# Software distributed under the License is distributed on an "AS IS"
# basis, WITHOUT WARRANTY OF ANY KIND, either express or implied. See the
# License for the specific language governing rights and limitations
# under the License.
# 
# The Original Code is Komodo code.
# 
# The Initial Developer of the Original Code is ActiveState Software Inc.
# Portions created by ActiveState Software Inc are Copyright (C) 2000-2010
# ActiveState Software Inc. All Rights Reserved.
# 
# Contributor(s):
#   ActiveState Software Inc
# 
# Alternatively, the contents of this file may be used under the terms of
# either the GNU General Public License Version 2 or later (the "GPL"), or
# the GNU Lesser General Public License Version 2.1 or later (the "LGPL"),
# in which case the provisions of the GPL or the LGPL are applicable instead
# of those above. If you wish to allow use of your version of this file only
# under the terms of either the GPL or the LGPL, and not to allow others to
# use your version of this file under the terms of the MPL, indicate your
# decision by deleting the provisions above and replace them with the notice
# and other provisions required by the GPL or the LGPL. If you do not delete
# the provisions above, a recipient may use your version of this file under
# the terms of any one of the MPL, the GPL or the LGPL.
# 
# ***** END LICENSE BLOCK *****

from xpcom import components, ServerException, COMException, nsError
from xpcom._xpcom import PROXY_SYNC, PROXY_ALWAYS, PROXY_ASYNC, getProxyForObject
from xpcom.server import WrapObject, UnwrapObject
 
import json, sys, os, re, types, string, threading
from os.path import join
from koTreeView import TreeView

import eollib
import fileutils
import koToolbox2
from projectUtils import *

import logging

log = logging.getLogger("Toolbox2HTreeView")
#log.setLevel(logging.DEBUG)

eol = None
eol_re = re.compile(r'(?:\r?\n|\r)')
_tbdbSvc = None  # module-global handle to the database service
_view = None     # module-global handle to the tree-view (needs refactoring)

"""
Manage a hierarchical view of the loaded tools
"""

class _KoTool(object):
    _com_interfaces_ = [components.interfaces.koITool]
    isContainer = False
    def __init__(self, name, id):
        self.name = name
        self.id = id  # path_id in DB
        self.initialized = False
        self.value = ""
        self._attributes = {}
        self._nondb_attributes = {}
        self.flavors = ['text/unicode','application/x-komodo-part',\
# #if PLATFORM != "win"
                        # XXX for a later release, scintilla needs work in this area
                        'TEXT',#,'COMPOUND_TEXT','STRING','UTF-8' \
# #endif
                        ]

    def __str__(self):
        s = {}
        for name in ['value', 'name']:
            res = getattr(self, name, None)
            if res is not None:
                s[name] = res
        s.update(self._attributes)
        return str(s)
        
    def init(self):
        pass

    def getCustomIconIfExists(self):
        iconurl = _tbdbSvc.getCustomIconIfExists(self.id)
        if iconurl:
            self.set_iconurl(iconurl)
        return iconurl is not None

    def updateKeyboardShortcuts(self):
        res = _tbdbSvc.getValuesFromTableByKey('common_tool_details', ['keyboard_shortcut'], 'path_id', self.id)
        if res is not None:
            self.setStringAttribute('keyboard_shortcut', res[0])

    def _finishUpdatingSelf(self, info):
        for name in ['value', 'name']:
            if name in info:
                setattr(self, name, info[name])
                del info[name]
        for key, value in info.items():
            self._attributes[key] = value
        self.initialized = True

    #non-xpcom
    def fillDetails(self, itemDetailsDict):
        non_attr_names = ['name', 'id']
        for name in non_attr_names:
            res = getattr(self, name, None)
            if res is not None:
                itemDetailsDict[name] = res
        itemDetailsDict['type'] = self.get_toolType()
        itemDetailsDict['value'] = self.value.split(eol)
        for name in self._attributes:
            itemDetailsDict[name] = self._attributes[name]

    # Attributes...
    def get_toolType(self):
        return self.typeName

    def get_value(self):
        return self.value

    def get_path(self):
        return _tbdbSvc.getPath(self.id)

    def set_path(self, val):
        raise ServerException(nsError.NS_ERROR_ILLEGAL_VALUE,
                              ("can't call setpath on %s %s (id %r)"
                               % (self.get_toolType(), self.typeName, self.id)))

    def get_parent(self):
        res = _tbdbSvc.getValuesFromTableByKey('hierarchy',
                                               ['parent_path_id'],
                                               'path_id', self.id)
        if res is None or res[0] is None:
            return None
        return _view.getToolById(res[0])        
                                  
    def hasAttribute(self, name):
        # Keep names out of attributes
        if name == 'name':
            return self.name is not None
        return self._attributes.has_key(name)

    def getAttribute(self, name):
        if not self.hasAttribute(name):
            self.updateSelf()
        return self._attributes[name]

    def setAttribute(self, name, value):
        if name not in self._attributes or self._attributes[name] != value: # avoid dirtification when possible.
            self._attributes[name] = value

    def removeAttribute(self, name):
        if name not in self._attributes: return
        del self._attributes[name]

    def getStringAttribute(self, name):
        try:
            return unicode(self._attributes[name])
        except KeyError:
            if name == "name":
                return self.name
            raise

    def setStringAttribute(self, name, value):
        # Keep names out of attributes
        if name == 'name':
            self.name = value
        else:
            self.setAttribute(name, unicode(value))

    def getLongAttribute(self, name):
        return int(self.getAttribute(name))

    def setLongAttribute(self, name, value):
        self.setAttribute(name, int(value))

    def getBooleanAttribute(self, name):
        return self.getLongAttribute(name) and 1 or 0

    def setBooleanAttribute(self, name, value):
        self.setAttribute(name, value and 1 or 0)

    def get_prefset(self):
        #XXX TODO! Work out where prefs live in toolbox2
        prefset = components.classes["@activestate.com/koPreferenceSet;1"] \
                  .createInstance(components.interfaces.koIPreferenceSet)
        return prefset

    def saveToolToDisk(self):
        if 'path' not in self._nondb_attributes:
            self._nondb_attributes['path'] = _tbdbSvc.getPath(self.id)
        path = self._nondb_attributes['path']
        fp = open(path, 'r')
        data = json.load(fp, encoding="utf-8")
        fp.close()
        data['value'] = self.value.split(eol)
        data['name'] = self.name
        for name in self._attributes:
            data[name] = self._attributes[name]
        fp = open(path, 'w')
        data = json.dump(data, fp, encoding="utf-8", indent=2)
        fp.close()

    def saveNewToolToDisk(self, path):
        data = {}
        data['value'] = eol_re.split(self.value)
        data['type'] = self.typeName
        data['name'] = self.name
        for name in self._attributes:
            data[name] = self._attributes[name]
        fp = open(path, 'w')
        data = json.dump(data, fp, encoding="utf-8", indent=2)
        fp.close()
        
    def saveContentToDisk(self):
        if 'path' not in self._nondb_attributes:
            self._nondb_attributes['path'] = _tbdbSvc.getPath(self.id)
        path = self._nondb_attributes['path']
        fp = open(path, 'r')
        data = json.load(fp, encoding="utf-8")
        fp.close()
        data['value'] = eol_re.split(self.value)
        data['type'] = self.typeName
        data['name'] = getattr(self, 'name', data['name'])
        for attr in self._attributes:
            newVal = self._attributes[attr]
            if attr not in data or newVal != data[attr]:
                data[attr] = self._attributes[attr]
        fp = open(path, 'w')
        data = json.dump(data, fp, encoding="utf-8", indent=2)
        fp.close()

    def save_handle_attributes(self):
        names = ['name', 'value']
        for name in names:
            if name in self._attributes:
                log.debug("Removing self._attributes %s = %s", name,
                          self._attributes[name])
                setattr(self, name, self._attributes[name])
                del self._attributes[name]

    def delete(self):
        path = self.get_path()
        _tbdbSvc.deleteItem(self.id)
        # On Windows this moves the full folder to the recycle bin
        # as an atomic unit, so the user can get it back, but can't
        # examine its contents.  Better than flattening a tree out
        # and putting each item in the bin's top-level.
        sysUtilsSvc = components.classes["@activestate.com/koSysUtils;1"].\
                      getService(components.interfaces.koISysUtils);
        sysUtilsSvc.MoveToTrash(path)

    def save(self):
        raise ServerException(nsError.NS_ERROR_ILLEGAL_VALUE,
                              ("save not yet implemented for %s"
                               % self.get_toolType()))

    def getFile(self):
        url = self.get_url()
        if url is not None:
            fsvc = components.classes["@activestate.com/koFileService;1"].getService(components.interfaces.koIFileService)
            return fsvc.getFileFromURI(url)
        return None

    def get_url(self):
        if self.hasAttribute('url'):
            return self.getStringAttribute('url')
        return None

    def get_id(self):
        return str(self.id)
    
    def get_keybinding_description(self):
        return self.prettytype + "s: " + self.name

    #TODO: Reinstate handling of relative uri's
    def get_iconurl(self):
        if self._attributes.has_key('icon'):
            return self._attributes['icon']
        else:
            return self._iconurl

    def set_iconurl(self, url):
        if not url or url == self._iconurl:
            self.removeAttribute('icon')
        else:
            self.setAttribute('icon', url)
            
    def get_type(self):
        return self.typeName

    def set_type(self, value):
        self.typeName = value
        
    def set_name(self, name):
        self.name = name

    def get_name(self):
        return self.name

    def added(self):
        # Are there any custom menus or toolbars that contain this item?
        id = self.id
        while True:
            parentRes = _tbdbSvc.getParentNode(id)
            if parentRes is None:
                break
            id, name, nodeType = parentRes
            if nodeType in ('menu', 'toolbar'):
                _observerSvc = components.classes["@mozilla.org/observer-service;1"]\
                               .getService(components.interfaces.nsIObserverService)
                try:
                    tool = _view.getToolById(id)
                    _observerSvc.notifyObservers(tool, nodeType + '_changed', '')
                except Exception:
                    pass
                break
            
class _KoContainer(_KoTool):
    isContainer = True
    def __init__(self, *args):
        self.childNodes = []
        self.isOpen = False
        _KoTool.__init__(self, *args)
        
    def init(self):
        if self.initialized:
            return
        self.childNodes = _tbdbSvc.getChildNodes(self.id)
        self.initialized = True

    def saveNewToolToDisk(self, path):
        raise Exception("Not implemented yet")

    def updateSelf(self):
        res = _tbdbSvc.getValuesFromTableByKey('common_details', ['name'], 'path_id', self.id)
        if res:
            self.name = res[0]
        else:
            self.name = "<Unknown item>"

class _KoFolder(_KoContainer):
    _com_interfaces_ = [components.interfaces.koIContainerBaseTool]
    typeName = 'folder'
    prettytype = 'Folder'
    _iconurl = 'chrome://komodo/skin/images/folder-closed-pink.png'

    def trailblazeForPath(self, path):
        os.mkdir(path)

    def getChildren(self):
        return [_view._getOrCreateTool(node[2], node[1], node[0]) for node in self.childNodes]
    
    def saveNewToolToDisk(self, path):
        # Folder has already been created.
        pass

    def save(self):
        # What about renaming a folder?
        pass

class _KoComplexContainer(_KoFolder):

    def __init__(self, *args):
        self.children = []
        #XXX: For menus, use .komodotools in folders to provide
        # order of items in nested menus.
        _KoFolder.__init__(self, *args)

    def trailblazeForPath(self, path):
        # Throw an error message if we can't create the file,
        # before we add an entry to the database.  But allow
        # for existing paths that weren't cleared earlier.
        if not os.path.exists(path):
            os.mkdir(path)
        elif not os.path.isdir(path):
            os.unlink(path)
            os.mkdir(path)
        path2 = join(path, koToolbox2.UI_FOLDER_FILENAME)
        fp = open(path2, 'w')
        fp.close()

    def saveNewToolToDisk(self, path):
        path2 = join(path, koToolbox2.UI_FOLDER_FILENAME)
        data = {}
        data['name'] = self.name
        data['type'] = self.typeName
        for name in self._attributes:
            data[name] = self._attributes[name]
        fp = open(path2, 'w')
        data = json.dump(data, fp, encoding="utf-8", indent=2)
        fp.close()

    def delete(self):
        notificationName = self.typeName + "_remove"
        _observerSvc = components.classes["@mozilla.org/observer-service;1"]\
                       .getService(components.interfaces.nsIObserverService)
        try:
            _observerSvc.notifyObservers(self, notificationName, notificationName)
        except Exception:
            pass
        _KoTool.delete(self)

class _KoMenu(_KoComplexContainer):
    typeName = 'menu'
    prettytype = 'Custom Menu'
    _iconurl = 'chrome://komodo/skin/images/menu_icon.png'
    
    def __init__(self, *args):
        _KoComplexContainer.__init__(self, *args)
        self._attributes['accesskey'] = ''
        self._attributes['priority'] = 100

    def save(self):
        self.save_handle_attributes()
        # What about renaming a folder?
        #self.saveToolToDisk()
        _tbdbSvc.saveMenuInfo(self.id, self.name, self._attributes)
        
class _KoToolbar(_KoComplexContainer):
    typeName = 'toolbar'
    prettytype = 'Custom Toolbar'
    _iconurl = 'chrome://komodo/skin/images/toolbar_icon.png'

    def __init__(self, *args):
        _KoComplexContainer.__init__(self, *args)
        self._attributes['priority'] = 100

    def save(self):
        self.save_handle_attributes()
        # What about renaming a folder?
        #self.saveToolToDisk()
        _tbdbSvc.saveToolbarInfo(self.id, self.name, self._attributes)

class _KoCommandTool(_KoTool):
    typeName = 'command'
    prettytype = 'Run Command'
    _iconurl = 'chrome://komodo/skin/images/run_commands.png'
    keybindable = 1        

    def save(self):
        # Write the changed data to the file system
        self.save_handle_attributes()
        self.saveToolToDisk()
        _tbdbSvc.saveCommandInfo(self.id, self.name, self.value, self._attributes)
    def updateSelf(self):
        if self.initialized:
            return
        info = _tbdbSvc.getCommandInfo(self.id)
        self._finishUpdatingSelf(info)

class _KoURL_LikeTool(_KoTool):
    def setStringAttribute(self, name, value):
        _KoTool.setStringAttribute(self, name, value)
        if name == 'value':
            # Komodo treats the value as a URI to get a koFileEx object.
            _KoTool.setStringAttribute(self, 'url', value)
            
    def save(self):
        self.save_handle_attributes()
        # Write the changed data to the file system
        self.saveToolToDisk()
        _tbdbSvc.saveSimpleToolInfo(self.id, self.name, self.value, self._attributes)
    def updateSelf(self):
        if self.initialized:
            return
        info = _tbdbSvc.getSimpleToolInfo(self.id)
        self._finishUpdatingSelf(info)

class _KoDirectoryShortcutTool(_KoURL_LikeTool):
    typeName = 'DirectoryShortcut'
    prettytype = 'Open... Shortcut'
    keybindable = 1
    _iconurl = 'chrome://komodo/skin/images/open.png'

class _KoMacroTool(_KoTool):
    _com_interfaces_ = [components.interfaces.koIMacroTool]
    typeName = 'macro'
    prettytype = 'Macro'
    _iconurl = 'chrome://komodo/skin/images/macro.png'
    keybindable = 1

    def __init__(self, *args):
        _KoTool.__init__(self, *args)
        self.flavors.insert(0,'text/x-moz-url')
        self._attributes['language'] = 'JavaScript'  # default.

    def delete(self):
        _observerSvc = components.classes["@mozilla.org/observer-service;1"]\
                       .getService(components.interfaces.nsIObserverService)
        try:
            _observerSvc.notifyObservers(self, 'macro-unload','')
        except Exception:
            pass
        _KoTool.delete(self)
        
    def updateSelf(self):
        if self.initialized:
            return
        info = _tbdbSvc.getMacroInfo(self.id)
        #log.debug("macro info: %s", info)
        self._finishUpdatingSelf(info)

    def get_url(self):
        if self._attributes['language'] == 'JavaScript':
            ext = ".js"
        elif self._attributes['language'] == 'Python':
            ext = ".py"
        return "macro2://%s/%s%s" % (self.id, self.name, ext)
        
    def save(self):
        # Write the changed data to the file system
        self.saveContentToDisk()
        _tbdbSvc.saveContent(self.id, self.value)
        _tbdbSvc.saveMacroInfo(self.id, self.name, self.value, self._attributes)

    def saveProperties(self):
        # Write the changed data to the file system
        self.saveToolToDisk()
        _tbdbSvc.saveMacroInfo(self.id, self.name, self.value, self._attributes)

    def _asyncMacroCheck(self, async):
        if async:
            lastErrorSvc = components.classes["@activestate.com/koLastErrorService;1"]\
                .getService(components.interfaces.koILastErrorService)
            err = "Asynchronous python macros not yet implemented"
            lastErrorSvc.setLastError(1, err)
            raise ServerException(nsError.NS_ERROR_ILLEGAL_VALUE, err)

    def evalAsPython(self, domdocument, window, scimoz, koDoc,
                     view, code, async):
        self._asyncMacroCheck(async)
        evalPythonMacro(WrapObject(self,self._com_interfaces_[0]),
                        domdocument, window, scimoz, koDoc, view, code)
        
    def evalAsPythonObserver(self, domdocument, window, scimoz, koDoc,
                             view, code, async, subject, topic, data):
        self._asyncMacroCheck(async)
        evalPythonMacro(WrapObject(self,self._com_interfaces_[0]),
                        domdocument, window, scimoz, koDoc, view, code,
                        subject, topic, data)

class _KoSnippetTool(_KoTool):
    typeName = 'snippet'
    prettytype = 'Snippet'
    _iconurl = 'chrome://komodo/skin/images/snippet.png'
    keybindable = 1

    def __init__(self, *args):
        _KoTool.__init__(self, *args)
        self.flavors.insert(0, 'application/x-komodo-snippet')

    def save(self):
        # Write the changed data to the file system
        self.save_handle_attributes()
        self.saveToolToDisk()
        _tbdbSvc.saveSnippetInfo(self.id, self.name, self.value, self._attributes)

    def updateSelf(self):
        if self.initialized:
            return
        info = _tbdbSvc.getSnippetInfo(self.id)
        self._finishUpdatingSelf(info)

class _KoTemplateTool(_KoURL_LikeTool):
    typeName = 'template'
    prettytype = 'Template'
    _iconurl = 'chrome://komodo/skin/images/newTemplate.png'

class _KoURLTool(_KoURL_LikeTool):
    typeName = 'URL'
    prettytype = 'URL'
    _iconurl = 'chrome://komodo/skin/images/xlink.png'
    keybindable = 1

class KoToolbox2HTreeView(TreeView):
    _com_interfaces_ = [components.interfaces.nsIObserver,
                        components.interfaces.koIToolbox2HTreeView,
                        components.interfaces.nsITreeView]
    _reg_clsid_ = "{7b345b58-bae7-4e0b-9a49-30119a1ffd29}"
    _reg_contractid_ = "@activestate.com/KoToolbox2HTreeView;1"
    _reg_desc_ = "KoToolbox2 Hierarchical TreeView"

    def __init__(self, debug=None):
        TreeView.__init__(self, debug=0)
        self._rows = []
        self._nodeOpenStatusFromName = {}
        self._tree = None
        self.toolbox_db = None
        self._tools = {}  # Map a tool's id to a constructed object
        global eol
        if eol is None:
            eol = eollib.eol2eolStr[eollib.EOL_PLATFORM]
        _observerSvc = components.classes["@mozilla.org/observer-service;1"].\
            getService(components.interfaces.nsIObserverService)
        _observerSvc.addObserver(self, 'toolbox-tree-changed', 0)
        _observerSvc.addObserver(self, 'xpcom-shutdown', 0)

    def _getOrCreateTool(self, node_type, name, path_id):
        tool = self._tools.get(path_id, None)
        if tool is not None:
            return tool
        tool = createPartFromType(node_type, name, path_id)
        tool.init()
        tool.getCustomIconIfExists()
        self._tools[path_id] = tool
        return tool

    def getToolById(self, path_id):
        path_id = int(path_id)
        if path_id not in self._tools:
            res = self.toolbox_db.getValuesFromTableByKey('common_details', ['type', 'name'], 'path_id', path_id)
            if res is None:
                return None
            tool_type, name = res
            tool = createPartFromType(tool_type, name, path_id)
            self._tools[path_id] = tool
        return self._tools[path_id]

    def getToolFromPath(self, path):
        res = self.toolbox_db.getValuesFromTableByKey('paths', ['id'], 'path', path)
        if not res:
            return None
        path_id = res[0]
        return self.getToolById(path_id)
    
    def getIndexByPath(self, path):
        # This way is slower...
        for i, tool in enumerate(self._rows):
            if tool.get_path() == path:
                return i
        return -1
    
    def getIndexByTool(self, tool):
        tool = UnwrapObject(tool)
        try:
            return self._rows.index(tool)
        except ValueError, e:
            return -1

    def selectedItemsHaveSameParent(self):
        # Ignore all child nodes in the selection
        treeSelection = self.selection
        selectedIndices = []
        parent_index = -2
        rangeCount = treeSelection.getRangeCount()
        for i in range(rangeCount):
            min_index, max_index = treeSelection.getRangeAt(i)
            index = min_index
            while index < max_index + 1:
                tool = self.getTool(index)
                res = self.toolbox_db.getValuesFromTableByKey('hierarchy',
                                                      ['parent_path_id'],
                                                      'path_id', tool.id)
                if not res:
                    if parent_index != -2:
                        return False
                else:
                    candidate_index = res[0]
                    if parent_index == -2:
                        parent_index = candidate_index
                    elif parent_index != candidate_index:
                        return False
                # And skip any selected children, if all are selected.
                if self.isContainerOpen(index):
                    nextSiblingIndex = self.getNextSiblingIndex(index)
                    if nextSiblingIndex <= max_index + 1:
                        index = nextSiblingIndex
                        continue  # don't increment at end of loop
                    elif (nextSiblingIndex == -1
                          and max_index == len(self._rows) - 1
                          and i == rangeCount - 1):
                        return True
                    else:
                        return False
                
                index += 1 # end while index < max_index + 1:
        return True

    # Is the node at row srcIndex an ancestor of the node at targetIndex?
    def isAncestor(self, srcIndex, targetIndex):
        if srcIndex < 0 or targetIndex < 0:
            return False
        elif srcIndex > targetIndex:
            return False
        elif srcIndex == targetIndex:
            return True
        
        srcLevel = self._rows[srcIndex].level
        while targetIndex > srcIndex and self._rows[targetIndex].level > srcLevel:
            targetIndex -= 1
        return targetIndex == srcIndex

    def createToolFromType(self, tool_type):
        id = self.toolbox_db.getNextID()
        tool = createPartFromType(tool_type, None, id)
        self._tools[id] = tool
        return tool

    def _prepareUniqueFileSystemName(self, dirName, baseName, addExt=True):
        # "slugify"
        basePart = koToolbox2.truncateAtWordBreak(re.sub(r'[^\w\d\-=\+]+', '_', baseName))
        basePart = join(dirName, basePart)
        extPart = (addExt and koToolbox2.TOOL_EXTENSION) or ""
        candidate = basePart + extPart
        if not os.path.exists(candidate):
            return candidate
        for i in range(1, 1000):
            candidate = "%s-%d%s" % (basePart, i, extPart)
            if not os.path.exists(candidate):
                log.debug("Writing out file %s/%s as %s", os.getcwd(), basePart, candidate)
                return candidate
        else:
            raise Exception("File %s exists in directory %s, force is off" %
                            (name, os.getcwd()))

    def addNewItemToParent(self, parent, item, showNewItem=True):
        #TODO: if parent is null, use the std toolbox node
        item = UnwrapObject(item)
        parent = UnwrapObject(parent)
        parent_path = self.toolbox_db.getPath(parent.id)
        item_name = item.name
        itemIsContainer = item.isContainer
        if itemIsContainer:
            # Don't do anything to this name.  If there's a dup, or
            # it contains bad characters, give the user the actual
            # error message.  Which is why we need to try creating
            # the folder first, before adding its entry.
            path = join(parent_path, item_name)
            item.trailblazeForPath(path)
        else:
            path = self._prepareUniqueFileSystemName(parent_path, item_name, addExt=True)
        try:
            itemDetailsDict = {}
            item.fillDetails(itemDetailsDict)
            if itemIsContainer:
                new_id = self.toolbox_db.addContainerItem(itemDetailsDict,
                                                          item.typeName,
                                                          path,
                                                          item_name,
                                                          parent.id)
            else:
                new_id = self.toolbox_db.addTool(itemDetailsDict,
                                                 item.typeName,
                                                 path,
                                                 item_name,
                                                 parent.id)
                
            old_id = item.id
            item.id = new_id
            if item.typeName == 'macro':
                _observerSvc = components.classes["@mozilla.org/observer-service;1"]\
                               .getService(components.interfaces.nsIObserverService)
                try:
                    _observerSvc.notifyObservers(item, 'macro-load','')
                except Exception:
                    pass
            try:
                del self._tools[old_id]
            except KeyError:
                log.error("No self._tools[%r]", old_id)
            self._tools[new_id] = item
            item.saveNewToolToDisk(path)
        except:
            log.exception("addNewItemToParent: failed")
            raise
        else:
            # Add and show the new item
            index = self.getIndexByTool(parent)
            UnwrapObject(parent).childNodes.append((new_id, item_name, item.typeName))
            if showNewItem:
                isOpen = self.isContainerOpen(index)
                if isOpen:  #TODO: Make this a pref?
                    firstVisibleRow = self._tree.getFirstVisibleRow()
                    self._tree.scrollToRow(firstVisibleRow)
                    # Easy hack to resort the items
                    self.toggleOpenState(index)
                    self.toggleOpenState(index, suppressUpdate=True)
                    self._tree.scrollToRow(firstVisibleRow)
                    try:
                        index = self._rows.index(item, index + 1)
                    except ValueError:
                        pass
                else:
                    self.toggleOpenState(index)
                newIndex = self.getIndexByPath(path)
                if newIndex >= 0:
                    index = newIndex
                self.selection.currentIndex = index
                self.selection.select(index)
                self._tree.ensureRowIsVisible(index)
        item.added()

    def deleteToolAt(self, index):
        self._tree.beginUpdateBatch()
        try:
            if self.isContainerOpen(index):
                self.toggleOpenState(index)
            tool = self._rows[index]
            tool_id = tool.id
            try:
                del self._tools[tool_id]
            except KeyError:
                pass
            res = self.toolbox_db.getValuesFromTableByKey('hierarchy',
                                                          ['parent_path_id'],
                                                          'path_id', tool_id)
            if res:
                parent_tool = self._tools.get(res[0], None)
            else:
                parent_tool = None
            tool.delete()
            try:
                del self._nodeOpenStatusFromName[self._rows[index].get_path()]
            except KeyError:
                pass
            del self._rows[index]
            self._tree.rowCountChanged(index, -1)
        finally:
            self._tree.endUpdateBatch()
        if parent_tool:
            for i, node in enumerate(parent_tool.childNodes):
                if node[0] == tool_id:
                    del parent_tool.childNodes[i]
                    break
            else:
                log.debug("Failed to find a child node in parent %d", res[0])

    def copyLocalFolder(self, srcPath, targetDirPath):
        fileutils.copyLocalFolder(srcPath, targetDirPath)
        
    def pasteItemsIntoTarget(self, targetIndex, paths, copying):
        targetTool = self.getTool(targetIndex)
        if not targetTool.isContainer:
            raise Exception("pasteItemsIntoTarget: item at row %d isn't a container, it's a %s" %
                            (targetIndex, targetTool.typeName))
        targetPath = targetTool.get_path()
        targetId = self.toolbox_db.get_id_from_path(targetPath)
        if targetId is None:
            raise Exception("target %s (%d) isn't in the database" % (
                targetPath, targetIndex
            ))
        if not copying:
            self._tree.beginUpdateBatch()
        try:
            for path in paths:
                if not os.path.exists(path):
                    #TODO: Bundle all the problems into one string that gets raised back.
                    log.debug("Path %s doesn't exist", path)
                if os.path.isdir(path):
                    self._pasteContainerIntoTarget(targetId, targetPath, targetTool, path, copying)
                else:
                    self._pasteItemIntoTarget(targetPath, targetTool, path, copying)
                if path != paths[-1] and self._rows[targetIndex].get_path() != targetPath:
                    # We need to readjust the targetIndex, as something moved
                    if copying:
                        log.debug("pasteItemsIntoTarget: Unexpected: target moved while copying")
                    if targetIndex == 0:
                        log.debug("pasteItemsIntoTarget: Unexpected: targetIndex of 0 changed during a move with copying=%r", copying)
                    else:
                        if self._rows[targetIndex - 1].get_path() == targetPath:
                            targetIndex -= 1
        finally:
            if not copying:
                self._tree.endUpdateBatch()
        self.refreshFullView() #TODO: refresh only parent nodes
        self._tree.ensureRowIsVisible(targetIndex)
                
    def _pasteItemIntoTarget(self, targetPath, targetTool, srcPath, copying):
        # Only sanity check is for copying/moving into current location.
        # Otherwise no check for leaves, like are we copying
        # into a ancestor node (who cares), or is the target a child of the src
        # -- that's impossible
        if targetPath == os.path.dirname(srcPath):
            log.debug("Skipping a self copy of %s into %s", srcPath, targetPath)
            return
        try:
            fp = open(srcPath, 'r')
        except:
            log.error("Failed to open %s", srcPath)
            return
        try:
            data = json.load(fp, encoding="utf-8")
        except:
            log.exception("Failed to json load %s", srcPath)
            return
        finally:
            fp.close()
        try:
            typeName = data['type']
        except KeyError:
            #XXX: Collect this, alert the user
            log.error("_pasteItemIntoTarget: no type field in tool %s", srcPath)
            return
        try:
            del data['id']
        except KeyError:
            pass
        newItem = self.createToolFromType(data['type'])
        if 'value' in data:
            # Value strings in .kotool files are stored as lines without CRs
            # In the database they're stored as a single string.
            # Convert into the database type, as that's what
            # _finishUpdatingSelf expects
            data['value'] = eol.join(data['value'])
        newItem._finishUpdatingSelf(data)
        self.addNewItemToParent(targetTool, newItem, showNewItem=False)
        if not copying:
            self._removeItemByPath(srcPath)
            
    def _pasteContainerIntoTarget(self, targetId, targetPath, targetTool, srcPath, copying):
        # Sanity checks:
        # Don't paste an item into its child
        #    Includes: don't paste an item onto itself.
        if targetPath == os.path.dirname(srcPath):
            log.debug("Skipping child copy of %s into %s", srcPath, targetPath)
            return
        #TODO: Support Menus and Toolbars!
        newItem = self.createToolFromType('folder')
        data = {'name':os.path.basename(srcPath)}
        newItem._finishUpdatingSelf(data)
        self.addNewItemToParent(targetTool, newItem, showNewItem=False)

        # Now we're copying the source's children into the newly created target child
        newTargetPath = join(targetPath, os.path.basename(srcPath))
        newTargetId = self.toolbox_db.get_id_from_path(newTargetPath)
        if newTargetId is None:
            raise Exception("new target %s isn't in the database" % (
                newTargetPath
            ))
        newTargetTool = self.getToolById(newTargetId)
        for childFile in os.listdir(srcPath):
            newSrcPath = join(srcPath, childFile)
            if os.path.isdir(newSrcPath):
                self._pasteContainerIntoTarget(newTargetId, newTargetPath, newTargetTool, newSrcPath, copying)
            else:
                self._pasteItemIntoTarget(newTargetPath, newTargetTool, newSrcPath, copying)
        if not copying:
            self._removeItemByPath(srcPath)

    def _removeItemByPath(self, srcPath):
        # Remove the tool from the self._tools cache
        src_id = self.toolbox_db.get_id_from_path(srcPath)
        if src_id is None:
            log.debug("_removeItemByPath: no id for path %s", srcPath)
            return
        srcTool = self.getToolById(src_id)
        if srcTool is None:
            log.debug("_removeItemByPath: no tool for id %d", src_id)
            return
        try:
            del self._tools[src_id]
        except KeyError:
            pass

        # Remove the tool from the tree view
        srcIndex = self.getIndexByTool(srcTool)
        if srcIndex == -1:
            srcIndex = self.getIndexByPath(srcPath)
        if srcIndex >= 0:
            self.deleteToolAt(srcIndex)

    def _zipNode(self, zf, currentDirectory):
        nodes = os.listdir(currentDirectory)
        numZippedItems = 0
        for path in nodes:
            fullPath = join(currentDirectory, path)
            # these filenames should be "sluggified" already,
            # although maybe not the dirnames.
            relativePath = fullPath[self._targetZipFileRootLen:]
            if os.path.isfile(fullPath):
                zf.write(fullPath, relativePath)
                numZippedItems += 1
            elif os.path.isdir(fullPath) and not os.path.islink(fullPath):
                numZippedItems += self._zipNode(zf, fullPath)
        return numZippedItems

    def zipSelectionToFile(self, targetZipFile):
        selectedIndices = self.getSelectedIndices(rootsOnly=True)
        import zipfile
        zf = zipfile.ZipFile(targetZipFile, 'w')
        numZippedItems = 0
        for index in selectedIndices:
            tool = self.getTool(index)
            path = self.toolbox_db.getPath(tool.id)
            if tool.isContainer and path[-1] in "\\/":
                path = path[:-1]
            self._targetZipFileRootLen = len(os.path.dirname(path)) + 1
            if not tool.isContainer:
                zf.write(path, path[self._targetZipFileRootLen:])
                numZippedItems += 1
            else:
                numZippedItems += self._zipNode(zf, path)
        return numZippedItems

    def getSelectedIndices(self, rootsOnly=False):
        treeSelection = self.selection
        selectedIndices = []
        numRanges = treeSelection.getRangeCount()
        for i in range(numRanges):
            min_index, max_index = treeSelection.getRangeAt(i)
            index = min_index
            while index < max_index + 1:
                selectedIndices.append(index)
                if rootsOnly and self.isContainerOpen(index):
                    nextSiblingIndex = self.getNextSiblingIndex(index)
                    if nextSiblingIndex <= max_index + 1:
                        index = nextSiblingIndex - 1
                    else:
                        if nextSiblingIndex == -1 and i < numRanges - 1:
                            raise ServerException(nsError.NS_ERROR_ILLEGAL_VALUE,
                              ("node at row %d supposedly at end, but we're only at range %d of %d" %
                               (j, i + 1, numRanges)))
                        index = max_index
                index += 1
        return selectedIndices

    def _getFullyRealizedToolById(self, ids):
        # We load tools lazily, so make sure these have the keyboard
        # shortcuts and other info in them.
        tools = []
        for id in ids:
            tool = self.getToolById(id)
            if tool:
                # Trigger macros need to be fully initialized,
                # since they can be invoked
                tool.updateSelf()
                tools.append(tool)
        return tools

    def getAbbreviationSnippet(self, abbrev, subnames):
        id = self.toolbox_db.getAbbreviationSnippetId(abbrev, subnames)
        if id is None:
            return None
        tool = self.getToolById(id)
        if tool:
            # Snippets need to be fully initialized
            tool.updateSelf()
        return tool
    
    def getToolsWithKeyboardShortcuts(self, dbPath):
        ids = self.toolbox_db.getIDsForToolsWithKeyboardShortcuts(dbPath)
        return self._getFullyRealizedToolById(ids)
    
    def getCustomMenus(self, dbPath):
        ids = self.toolbox_db.getIDsByType('menu', dbPath)
        for id in ids:
            self._fillChildren(id)
        return self._getFullyRealizedToolById(ids)
    
    def getCustomToolbars(self, dbPath):
        ids = self.toolbox_db.getIDsByType('toolbar', dbPath)
        for id in ids:
            self._fillChildren(id)
        return self._getFullyRealizedToolById(ids)

    def _fillChildren(self, id):
        tool = self.getToolById(id)
        tool.init()
        for child_id, _, _ in tool.childNodes:
            child = self.getToolById(child_id)
            if child.isContainer:
                self._fillChildren(child_id)
    
    def getTriggerMacros(self, dbPath):
        ids = self.toolbox_db.getTriggerMacroIDs(dbPath)
        return self._getFullyRealizedToolById(ids)
    
    def refreshFullView(self):
        i = 0
        lim = len(self._rows)
        self._tree.beginUpdateBatch();
        try:
            while i < lim:
                before_len = len(self._rows)
                if self.isContainerOpen(i):
                    before_len = len(self._rows)
                    self.toggleOpenState(i)
                    self.toggleOpenState(i, suppressUpdate=True)
                else:
                    try:
                        if self._nodeOpenStatusFromName[self._rows[i].get_path()]:
                            self.toggleOpenState(i, suppressUpdate=True)
                    except KeyError:
                        pass
                after_len = len(self._rows)
                delta = after_len - before_len
                lim += delta
                i += delta + 1
        finally:
            self._tree.endUpdateBatch();
            
    def refreshView(self, index):
        self._tree.beginUpdateBatch();
        try:
            if self.isContainerOpen(index):
                self.toggleOpenState(index)
                self.toggleOpenState(index, suppressUpdate=True)
            elif self._nodeOpenStatusFromName[self._rows[index].get_path()]:
                self.toggleOpenState(index, suppressUpdate=True)
        finally:
            self._tree.endUpdateBatch();
        
    def initialize(self):
        prefs = components.classes["@activestate.com/koPrefService;1"].\
            getService(components.interfaces.koIPrefService).prefs
        if not prefs.hasPref("toolbox2"):
            toolboxPrefs = components.classes["@activestate.com/koPreferenceSet;1"].createInstance()
            prefs.setPref("toolbox2", toolboxPrefs)
        else:
            toolboxPrefs = prefs.getPref("toolbox2")
        if toolboxPrefs.hasPref("open-nodes"):
            self._nodeOpenStatusFromName = json.loads(toolboxPrefs.getStringPref("open-nodes"))
        else:
            self._nodeOpenStatusFromName = {}
        koToolBox2Svc = UnwrapObject(components.classes["@activestate.com/koToolBox2Service;1"].getService(components.interfaces.koIToolBox2Service))

        global _tbdbSvc, _view
        _tbdbSvc = self.toolbox_db = UnwrapObject(components.classes["@activestate.com/KoToolboxDatabaseService;1"].\
                       getService(components.interfaces.koIToolboxDatabaseService))
        _view = self

        self.toolbox_db.toolManager = self

        toolboxLoader = koToolBox2Svc.toolboxLoader
        toolboxLoader.markAllTopLevelItemsUnloaded()
        
        koDirSvc = components.classes["@activestate.com/koDirs;1"].getService()
        stdToolboxDir = join(koDirSvc.userDataDir,
                                     koToolbox2.DEFAULT_TARGET_DIRECTORY)
        if not os.path.exists(stdToolboxDir):
            try:
                os.mkdir(stdToolboxDir)
            except:
                log.error("Can't create tools dir %s", stdToolboxDir)
        import time
        t1 = time.time()
        toolbox_id = toolboxLoader.loadToolboxDirectory("Standard Toolbox", stdToolboxDir, koToolbox2.DEFAULT_TARGET_DIRECTORY)
        koToolBox2Svc.notifyAddedToolbox(stdToolboxDir)
        t2 = time.time()
        log.debug("Time to load std-toolbox: %g msec", (t2 - t1) * 1000.0)
        koToolBox2Svc.registerStandardToolbox(toolbox_id)

        extensionDirs = UnwrapObject(components.classes["@python.org/pyXPCOMExtensionHelper;1"].getService(components.interfaces.pyIXPCOMExtensionHelper)).getExtensionDirectories()
        for fileEx in extensionDirs:
            # Does this happen for disabled extensions?
            toolDir = join(fileEx.path, koToolbox2.DEFAULT_TARGET_DIRECTORY)
            if os.path.exists(toolDir):
                koToolBox2Svc.activateExtensionToolbox(fileEx.path,
                                                       koToolbox2.DEFAULT_TARGET_DIRECTORY)
                                                       
            #else:
            #    log.debug("No tools in %s", fileEx.path)
        
        toolboxLoader.deleteUnloadedTopLevelItems()
        self._redoTreeView()
        self._restoreView()
        self._tree.invalidate()

    def observe(self, subject, topic, data):
        if not topic:
            return
        if topic == 'toolbox-tree-changed':
            self._redoTreeView()
        elif topic == 'xpcom-shutdown':
            _observerSvc = components.classes["@mozilla.org/observer-service;1"].\
                getService(components.interfaces.nsIObserverService)
            _observerSvc.removeObserver(self, 'toolbox-tree-changed')
            _observerSvc.removeObserver(self, 'xpcom-shutdown')

    def _redoTreeView(self):
        self._tree.beginUpdateBatch()
        try:
            self._redoTreeView1_aux()
        finally:
            pass
            self._tree.endUpdateBatch()
        self.refreshFullView()

    def _redoTreeView1_aux(self):
        before_len = len(self._rows)
        top_level_nodes = self.toolbox_db.getTopLevelNodes()
        top_level_ids = [x[0] for x in top_level_nodes]
        index = 0
        lim = before_len
        while index < lim:
            id = int(self._rows[index].id)
            nextIndex = self.getNextSiblingIndex(index)
            if nextIndex == -1:
                finalIndex = lim
            else:
                finalIndex = nextIndex
            if id in top_level_ids:
                del top_level_ids[top_level_ids.index(id)]
                index = finalIndex
            else:
                if nextIndex == -1:
                    del self._rows[index:]
                else:
                    del self._rows[index: nextIndex]
                numNodesRemoved = finalIndex - index
                self._tree.rowCountChanged(index, -numNodesRemoved)
                lim -= numNodesRemoved
        
        before_len = len(self._rows)
        for path_id, name, node_type in top_level_nodes:
            if path_id not in top_level_ids:
                #log.debug("No need to reload tree %s", name)
                continue
            toolPart = self._getOrCreateTool(node_type, name, path_id)
            toolPart.level = 0
            self._rows.append(toolPart)
        after_len = len(self._rows)
        self._tree.rowCountChanged(0, after_len - before_len)
        
    def _restoreView(self):

        toolboxPrefs = components.classes["@activestate.com/koPrefService;1"].\
            getService(components.interfaces.koIPrefService).prefs.getPref("toolbox2")
        if toolboxPrefs.hasPref("firstVisibleRow"):
            firstVisibleRow = toolboxPrefs.getLongPref("firstVisibleRow")
        else:
            firstVisibleRow = -1
        greatestPossibleFirstVisibleRow = len(self._rows) - self._tree.getPageLength()
        if greatestPossibleFirstVisibleRow < 0:
            greatestPossibleFirstVisibleRow = 0
        if firstVisibleRow > greatestPossibleFirstVisibleRow:
            firstVisibleRow = greatestPossibleFirstVisibleRow
        
        if toolboxPrefs.hasPref("currentIndex"):
            currentIndex = toolboxPrefs.getLongPref("currentIndex")
        else:
            currentIndex = -1
        if currentIndex >= len(self._rows):
           currentIndex =  len(self._rows) - 1

        if firstVisibleRow != -1:
            self._tree.scrollToRow(firstVisibleRow)
        if currentIndex != -1:
            self.selection.currentIndex = currentIndex
            self._tree.ensureRowIsVisible(currentIndex)

    def terminate(self):
        prefs = components.classes["@activestate.com/koPrefService;1"].\
            getService(components.interfaces.koIPrefService).prefs
        try:
            toolboxPrefs = prefs.getPref("toolbox2")
            toolboxPrefs.setStringPref("open-nodes",
                                       json.dumps(self._nodeOpenStatusFromName))
            toolboxPrefs.setLongPref("firstVisibleRow",
                                     self._tree.getFirstVisibleRow())
            toolboxPrefs.setLongPref("currentIndex",
                                     self.selection.currentIndex)
        except:
            log.exception("problem in terminate")

    def getTool(self, index):
        if index < 0: return None
        try:
            tool = self._rows[index]
        except IndexError:
            log.error("Failed getTool(index:%d)", index)
            return None
        if not tool.initialized:
            tool.updateSelf()
        return tool

    def get_toolType(self, index):
        if index == -1:
            return None
        return self._rows[index].typeName
        
    #---- nsITreeView Methods
    
    def get_rowCount(self):
        return len(self._rows)

    def getCellText(self, index, column):
        col_id = column.id
        assert col_id == "Name"
        #log.debug(">> getCellText:%d, %s", row, col_id)
        try:
            return self._rows[index].name
        except AttributeError:
            log.debug("getCellText: No id %s at row %d", col_id, row)
            return "?"
        
    def getImageSrc(self, index, column):
        col_id = column.id
        assert col_id == "Name"
        try:
            return self._rows[index].get_iconurl()
        except:
            return ""
        
    def isContainer(self, index):
        try:
            return self._rows[index].isContainer
        except IndexError:
            log.error("isContainer[index:%d]", index)
            return False
        
    def isContainerOpen(self, index):
        node = self._rows[index]
        return node.isContainer and node.isOpen
        
    def isContainerEmpty(self, index):
        node = self._rows[index]
        return node.isContainer and not node.childNodes

    def getParentIndex(self, index):
        if index >= len(self._rows) or index < 0: return -1
        try:
            i = index - 1
            level = self._rows[index].level
            while i >= 0 and self._rows[i].level >= level:
                i -= 1
        except IndexError:
            i = -1
        return i

    def hasNextSibling(self, index, afterIndex):
        if index >= len(self._rows) or index < 0: return 0
        try:
            current_level = self._rows[index].level
            for next_row in self._rows[afterIndex + 1:]:
                next_row_level = next_row.level
                if next_row_level < current_level:
                    return 0
                elif next_row_level == current_level:
                    return 1
        except IndexError:
            pass
        return 0
    
    def getLevel(self, index):
        try:
            return self._rows[index].level
        except IndexError:
            return -1
                                                
    def setTree(self, tree):
        self._tree = tree
        
    def getNextSiblingIndex(self, index):
        """
        @param index {int} points to the node whose next-sibling we want to find.
        @return index of the sibling, or -1 if not found.
        """
        level = self._rows[index].level
        lim = len(self._rows)
        index += 1
        while index < lim:
            if self._rows[index].level <= level:
                return index
            index += 1
        return -1

    def toggleOpenState(self, index, suppressUpdate=False):
        rowNode = self._rows[index]
        if rowNode.isOpen:
            try:
                del self._nodeOpenStatusFromName[rowNode.get_path()]
            except KeyError:
                pass
            nextIndex = self.getNextSiblingIndex(index)
            #qlog.debug("toggleOpenState: index:%d, nextIndex:%d", index, nextIndex)
            if nextIndex == -1:
                # example: index=5, have 13 rows,  delete 7 rows [6:13), 
                numNodesRemoved = len(self._rows) - index - 1
                del self._rows[index + 1:]
            else:
                # example: index=5, have 13 rows, next sibling at index=10
                # delete rows [6:10): 4 rows
                numNodesRemoved = nextIndex - index - 1
                del self._rows[index + 1: nextIndex]
            #log.debug("toggleOpenState: numNodesRemoved:%d", numNodesRemoved)
            if numNodesRemoved:
                self._tree.rowCountChanged(index, -numNodesRemoved)
            rowNode.isOpen = False
        else:
            if not suppressUpdate:
                firstVisibleRow = self._tree.getFirstVisibleRow()
            before_len = len(self._rows)
            self._doContainerOpen(rowNode, index)
            after_len = len(self._rows)
            delta = after_len - before_len
            if delta:
                self._tree.rowCountChanged(index, delta)
            self._nodeOpenStatusFromName[rowNode.get_path()] = True
            if not suppressUpdate:
                self._tree.ensureRowIsVisible(firstVisibleRow)
                self.selection.select(index)

    def _doContainerOpen(self, rowNode, index):
        rowNode.init()
        childNodes = sorted(rowNode.childNodes, cmp=self._compareChildNode)
        if childNodes:
            posn = index + 1
            for path_id, name, node_type in childNodes:
                toolPart = self._getOrCreateTool(node_type, name, path_id)
                toolPart.level = rowNode.level + 1
                self._rows.insert(posn, toolPart)
                posn += 1
            rowNode.isOpen = True
            # Now open internal nodes working backwards
            lastIndex = index + len(childNodes)
            firstIndex = index
            # Work from bottom up so we don't have to readjust the index.
            for i, row in enumerate(self._rows[lastIndex: index: -1]):
                try:
                    openNode = self._nodeOpenStatusFromName[row.get_path()]
                except KeyError:
                    pass
                else:
                    if openNode:
                        self._doContainerOpen(row, lastIndex - i)
                
            
    def _compareChildNode(self, item1, item2):
        # TODO: Have a sort flag.
        # *Always* sort folders before other items.
        if item1[2] == 'folder':
            if item2[2] != 'folder':
                return -1
        elif item2[2] == 'folder':
            return 1
        return cmp(item1[1].lower(), item2[1].lower())

_partFactoryMap = {}
for name, value in globals().items():
    if isinstance(value, object) and getattr(value, 'typeName', ''):
        _partFactoryMap[value.typeName] = value

def createPartFromType(type, *args):
    if type == "project":
        project = _partFactoryMap[type]()
        project.create()
        return project
    return _partFactoryMap[type](*args)

