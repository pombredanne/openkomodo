#!/usr/bin/env python

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

"""Backend for new Komodo toolbox 

See KD 252 for details.

"""
import json
import os
from os.path import exists, join
import re
import sys
import shutil
import logging
from contextlib import contextmanager
import sqlite3
from pprint import pprint, pformat
import time

try:
    from xpcom import components
    from xpcom.server import UnwrapObject
    _xpcom_ = True
except ImportError:
    _xpcom_ = False



#---- globals

if not _xpcom_:
    logging.basicConfig()
log = logging.getLogger("koToolbox2")
#log.setLevel(logging.DEBUG)
eol = """
"""
_unsupported_types = ("file", "livefolder")
# for the std toolbox, shared toolboxes, and extensions
DEFAULT_TARGET_DIRECTORY = "tools"
# for toolboxes extracted from pre-v6 kpf files:
PROJECT_TARGET_DIRECTORY = ".komodotools"

TOOL_EXTENSION = ".komodotool"
UI_FOLDER_FILENAME = ".folderdata"

#---- errors

class Toolbox2Error(Exception):
    pass



#---- main module classes

class Database(object):
    """Wrapper API for the sqlite database of the history."""
    # Database version.
    # VERSION is the version of this Database code. The property
    # "version" is the version of the database on disk. The patch-level
    # version number should be used for small upgrades to the database.
    #
    # How to update version:
    # (a) change VERSION,
    # (b) add a change log comment here, and
    # (c) add an entry to `_upgrade_info_from_curr_ver`.
    #
    # db change log:
    # - 1.0.0: initial version
    VERSION = "1.0.5"
    
    def __init__(self, db_path, schemaFile=None):
        self.path = db_path
        self.cu = self.cx = None
        self.schemaFile = schemaFile
        if not exists(db_path):
            update_version = True
            self.create()
        else:
            update_version = False
            try:
                self.upgrade()
            except Exception, ex:
                log.exception("error upgrading `%s': %s", self.path, ex)
                self.reset()
        if update_version:
            self.set_meta("version", self.VERSION)

        self._specific_names = {
            'command':['insertOutput', 'parseRegex', 'operateOnSelection',
                     'doNotOpenOutputWindow', 'showParsedOutputList',
                     'parseOutput', 'runIn', 'cwd', 'env', ],
            'macro':['async', 'trigger_enabled', 'trigger',
                          'language', 'rank'],
            'snippet':['set_selection', 'indent_relative'],
            'menu':['accesskey', 'priority'],
            'toolbar':['priority'],
            'folder':[],
            }
            
    def __repr__(self):
        return "<Toolbox2 Database %s>" % self.path
    
    # Do this to wrap large numbers of transactions
    def establishConnection(self):
        self.cx = sqlite3.connect(self.path)
        self.cu = self.cx.cursor()

    def releaseConnection(self):
        self.cx.commit()
        self.cu.close()
        self.cx.close()
        self.cu = self.cx = None
            
    @contextmanager
    def connect(self, commit=False, cu=None):
        if self.cu:
            yield self.cu
        elif cu is not None:
            yield cu
        else:
            cx = sqlite3.connect(self.path)
            cu = cx.cursor()
            try:
                yield cu
            finally:
                if commit:
                    cx.commit()
                cu.close()
                cx.close()

    def create(self):
        """Create the database file."""
        #TODO: error handling?
        with self.connect(commit=True) as cu:
            f = open(self.schemaFile, 'r')
            cu.executescript(f.read())
            f.close()
            self.set_meta("version", self.VERSION, cu=cu)
            
    def upgrade(self):
        pass # Do nothing
        #XXX Finish this!
        
    def get_meta(self, key, default=None, cu=None):
        """Get a value from the meta table.
        
        @param key {str} The meta key.
        @param default {str} Default value if the key is not found in the db.
        @param cu {sqlite3.Cursor} An existing cursor to use.
        @returns {str} The value in the database for this key, or `default`.
        """
        with self.connect(cu=cu) as cu:
            cu.execute("SELECT value FROM toolbox2_meta WHERE key=?", (key,))
            row = cu.fetchone()
            if row is None:
                return default
            return row[0]
    
    def set_meta(self, key, value, cu=None):
        """Set a value into the meta table.
        
        @param key {str} The meta key.
        @param value {str} The value to associate with the key.
        @param cu {sqlite3.Cursor} An existing cursor to use.
        @returns {None}
        """
        with self.connect(commit=True, cu=cu) as cu:
            cu.execute("INSERT INTO toolbox2_meta(key, value) VALUES (?, ?)", 
                (key, value))

    def del_meta(self, key):
        """Delete a key/value pair from the meta table.
        
        @param key {str} The meta key.
        """
        with self.connect(commit=True) as cu:
            cu.execute("DELETE FROM toolbox2_meta WHERE key=?", (key,))

    #---- Accessor functions
    
    def getTopLevelPaths(self):
        with self.connect() as cu:
            cu.execute('''select p.path
                from paths as p, hierarchy as h
                where h.parent_path_id is null
                      and h.path_id == p.id''')
            rows = cu.fetchall()
            return [row[0] for row in rows]

    def getTopLevelNodes(self):
        """
        Return (path_id, name, nodeType of each top-level item)
        """
        with self.connect() as cu:
            cu.execute('''select cd.path_id, cd.name, cd.type
                from common_details as cd, hierarchy as h
                where h.parent_path_id is null
                      and h.path_id == cd.path_id''')
            return cu.fetchall()

    def getChildNodes(self, node_id):
        """
        Return (path_id, name, nodeType for each child)
        """
        with self.connect() as cu:
            cu.execute('''select cd.path_id, cd.name, cd.type
                from common_details as cd, hierarchy as h
                where h.parent_path_id == ? and cd.path_id == h.path_id''',
                       (node_id,))
            return cu.fetchall()
            
    def getChildIDs(self, node_id):
        with self.connect() as cu:
            cu.execute('''select path_id from hierarchy where parent_path_id == ?''',
                       (node_id,))
            return [x[0] for x in cu.fetchall()]

    def getParentNode(self, node_id):
        """
        Return (path_id, name, nodeType for each child)
        """
        with self.connect() as cu:
            cu.execute('''select cd.path_id, cd.name, cd.type
                from common_details as cd, hierarchy as h
                where h.path_id == ? and cd.path_id == h.parent_path_id''',
                       (node_id,))
            return cu.fetchone()

    def getTypeAndNameFromId(self, node_id):
        """
        Return (nodeType, name based on the ID)
        """
        with self.connect() as cu:
            cu.execute('''select cd.name, cd.type
                from common_details as cd, hierarchy as h
                where h.parent_path_id == ? and cd.path_id == h.path_id''',
                       (node_id,))
            return cu.fetchall()

    def getValuesFromTableByKey(self, table_name, columnNames, keyName, keyValue, cu=None):
        stmt = "select %s from %s where %s = ?" % \
               (", ".join(columnNames), table_name, keyName)
        with self.connect(cu=cu) as cu:
            try:
                cu.execute(stmt, (keyValue,))
            except sqlite3.OperationalError:
                log.debug("Couldn't do [%s]", stmt)
                raise
            return cu.fetchone()

    def _convertAndJoin(self, names, sep):
        # Return a string of form <<"name1 = ? <sep> name2 = ? ...">>
        return sep.join([("%s = ?" % name) for name in names])
        
    def deleteRowByKey(self, table_name, key_names, key_values, cu=None):
        condition = " and ".join(["%s = ?" % kname for kname in key_names])
        with self.connect(commit=True, cu=cu) as cu:
            cu.execute("delete from %s where %s" % (table_name, condition), key_values)

    def insertRowInTable(self, table_name, target_names, target_values, cu=None):
        cmd = "insert into %s (%s) values (%s)" % (table_name,
                                                   ", ".join(target_names),
                                                   ", ".join(['?'] * len(target_names)))
        with self.connect(commit=True, cu=cu) as cu:
            cu.execute(cmd, target_values)

    def updateValuesInTableByKey(self, table_name, target_names, target_values,
                                 key_names, key_values, cu=None):
        target_names_str = self._convertAndJoin(target_names, ",")
        key_names_str = self._convertAndJoin(key_names, " AND ")
        cmd = 'update %s set %s where %s' % (table_name, target_names_str,
                                             key_names_str)
        args = tuple(target_values + key_values)
        with self.connect(commit=True, cu=cu) as cu:
            try:
                cu.execute(cmd, args)
            except sqlite3.OperationalError:
                log.exception("Couldn't do [%s](%s)", stmt, args)
                return False
            else:
                return True
            
    def get_id_from_path(self, path):
        stmt = "select id from paths where path = ?"
        with self.connect() as cu:
            cu.execute(stmt, (path,))
            row = cu.fetchone()
            if row is None: return None
            return row[0]
            
    def getPath(self, id, cu=None):
        stmt = "select path from paths where id = ?"
        with self.connect(cu=cu) as cu:
            cu.execute(stmt, (id,))
            row = cu.fetchone()
            if row is None: return None
            return row[0]

    def getCustomIconIfExists(self, path_id):
        with self.connect() as cu:
            cu.execute("""select prop_value from misc_properties
                          where path_id = ? and prop_name = ?""",
                       (path_id, 'icon'))
            row = cu.fetchone()
            if not row:
                return None
            return row[0]

    def getNextID(self, cu=None):
        with self.connect(cu=cu) as cu:
            cu.execute('select seq from sqlite_sequence where name=?', ('paths',))
            return cu.fetchone()[0] + 1
    
    # Adder functions

    def _addCommonDetails(self, path, name, item_type, parent_path_id, cu):
        stmt = 'insert into paths(path) values(?)'
        cu.execute(stmt, (path,))
        id = cu.lastrowid
        stmt = '''insert into common_details
                (path_id, name, type) values (?, ?, ?)'''
        cu.execute(stmt, (id, name, item_type))
        try:
            if parent_path_id is None and item_type == "folder":
                stmt = 'insert into hierarchy(path_id) values(?)'
                cu.execute(stmt, (id,))
            else:
                stmt = 'insert into hierarchy(path_id, parent_path_id) values(?, ?)'
                #log.debug("About to insert id:%r, parent_id:%r", id, parent_path_id)
                cu.execute(stmt, (id, parent_path_id))
        except:
            pass # Stop here
            raise
        return id

    def addFolder(self, path, name, parent_path_id):
        #log.debug("About to add folder %s in %s", name, path)
        with self.connect(commit=True) as cu:
            metadataPath = join(path, UI_FOLDER_FILENAME)
            if exists(metadataPath):
                fp = open(metadataPath, 'r')
                data = json.load(fp, encoding="utf-8")
                fp.close()
                self._addCompoundItem(path, name, data, parent_path_id, cu)
                return
            id = self._addCommonDetails(path, name, 'folder', parent_path_id, cu)

    def addContainerItem(self, data, item_type, path, fname, parent_path_id):
        with self.connect(commit=True) as cu:
            id = self._addCommonDetails(path, fname, item_type, parent_path_id, cu)
            names = self._specific_names[item_type]
            if names:
                final_values = [id]
                final_names = ['path_id']
                for name in names:
                    res = data.get(name, None)
                    if res is not None:
                        final_names.append(name)
                        final_values.append(res)
                questions = ", ".join(["?"] * (len(final_names)))
                stmt = 'insert into %s(%s) values(%s)' % \
                       (item_type, ", ".join(final_names), questions)
                cu.execute(stmt, final_values)
        return id
                
    def _addCompoundItem(self, path, name, data, parent_path_id, cu):
        if name != data['name']:
            log.error("Bad compound item data: for item named %s, metadata is %s",
                      name, data['name'])
        node_type = data['type']
        # Process the children in the directory
        id = self._addCommonDetails(path, name, node_type, parent_path_id, cu)
        if node_type == 'menu':
            stmt = 'insert into menu(path_id, accessKey, priority) values(?, ?, ?)'
            cu.execute(stmt, (id, data.get('accesskey', ""), data.get('priority', 100)))
        elif node_type == 'toolbar':
            stmt = 'insert into toolbar(path_id, priority) values(?, ?)'
            cu.execute(stmt, (id, data.get('priority', 100)))
        else:
            log.error("Got an unexpected node type of %s", node_type)

    def finishAddingCompoundItem(self, item_type, id, name, path,
                                 attributes, parent_id):
        with self.connect(commit=True) as cu:
            self._addCommonDetails(path, name, item_type, parent_id, cu)
            names = self._specific_names[item_type]
    
    def addTool(self, data, item_type, path, fname, parent_path_id):
        # These should be in every item, and we don't want to keep them around
        if item_type in _unsupported_types:
            log.info("Dropping old-style tool type:%s, name:%s", item_type, fname)
            return # Goodbye
        pretty_name = data['name']
        id = data.get('id', None)
        common_names = ['name', 'type', 'id']
        #log.debug("About to add tool %s in %s", fname, path)
        for name in common_names:
            try:
                del data[name]
            except KeyError:
                #log.debug("key %s not in tool %s(type %s)", name, fname, item_type)
                pass
        with self.connect(commit=True) as cu:
            id = self._addCommonDetails(path, pretty_name, item_type, parent_path_id, cu)
            prefix = '_add_'
            toolMethod = getattr(self, prefix + item_type, None)
            if not toolMethod:
                toolMethod = getattr(self, prefix + 'genericTool')
            toolMethod(id, data, item_type, cu)
            return id
            
    def _getValuesFromDataAndDelete(self, id, data, names_and_defaults):
        valueList = [id]
        for key, default_value in names_and_defaults:
            try:
                valueList.append(data[key])
                del data[key]
            except KeyError:
                valueList.append(default_value)
        return valueList
            
    def _add_command(self, id, data, item_type, cu):
        self.addCommonToolDetails(id, data, cu)
        names_and_defaults = [
            ('insertOutput', '0'),
            ('parseRegex', False),
            ('operateOnSelection', False),
            ('doNotOpenOutputWindow', False),
            ('showParsedOutputList', False),
            ('parseOutput', False),
            ("runIn", "command-output-window"),
            ('cwd', ''),
            ('env', ''),
            ]
        valueList = self._getValuesFromDataAndDelete(id, data, names_and_defaults)
        nameList = ", ".join(['path_id'] + [x[0] for x in names_and_defaults])
        stmt = '''insert into command(%s)
                  values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''' % (nameList,)
        cu.execute(stmt, valueList)
        if data:
            self.addMiscProperties(id, data, cu)
    
    def _add_genericTool(self, id, data, item_type, cu):
        log.debug("Handling generic tool for type %s", item_type)
        self.addCommonToolDetails(id, data, cu)
        if data:
            self.addMiscProperties(id, data, cu)
            
    def _add_macro(self, id, data, item_type, cu):
        self.addCommonToolDetails(id, data, cu)
        names_and_defaults = [
            ('async', ''),
            ('trigger_enabled', False),
            ('trigger', ''),
            ('language', 'JavaScript'),
            ('rank', 100)
            ]
        valueList = self._getValuesFromDataAndDelete(id, data, names_and_defaults)
        stmt = '''insert into macro(
                  path_id, async, trigger_enabled, trigger, language, rank)
                  values(?, ?, ?, ?, ?, ?)'''
        cu.execute(stmt, valueList)
        if data:
            self.addMiscProperties(id, data, cu)
            
    def _add_snippet(self, id, data, item_type, cu):
        self.addCommonToolDetails(id, data, cu)
        names_and_defaults = [
            ('set_selection', False),
            ('indent_relative', False),
            ]
        valueList = self._getValuesFromDataAndDelete(id, data, names_and_defaults)
        stmt = '''insert into snippet(
                  path_id, set_selection, indent_relative)
                  values(?, ?, ?)'''
        cu.execute(stmt, valueList)
        if data:
            self.addMiscProperties(id, data, cu)

    def _addSimpleItem(self, id, data, item_type, cu):
        self.addCommonToolDetails(id, data, cu)
        if data:
            self.addMiscProperties(id, data, cu)

    _add_DirectoryShortcut = _addSimpleItem
    _add_template = _addSimpleItem
    _add_URL = _addSimpleItem
            
    def addMiscProperties(self, id, data, cu):
        for key, value in data.items():
            log.debug("Adding misc. property %s:%s on id:%d", key, value, id)
            stmt = '''insert into misc_properties values(?, ?, ?)'''
            cu.execute(stmt, (id, key, value))

    def addCommonToolDetails(self, id, data, cu):
        names_and_defaults = [
            ('value', []),
            ('keyboard_shortcut', ''),
            ]
        values = self._getValuesFromDataAndDelete(id, data, names_and_defaults)
        _, content, keyboard_shortcut = values
        if not content:
            content = ""
        else:
            if not content[-1]:
                del content[-1]
            if content and not content[0]:
                del content[0]
            content = eol.join(content)
        stmt = '''insert into common_tool_details(path_id, value, keyboard_shortcut)
                  values(?, ?, ?)'''
        cu.execute(stmt, (id, content, keyboard_shortcut))
        
                
    def insertMenuItem(self, child_id, position):
        with self.connect(commit=True) as cu:
            stmt = 'insert into menuItem(path_id, position) values(?, ?)'
            #log.debug("About to insert id:%r", child_id)
            cu.execute(stmt, (child_id, position))
            
    def insertMetadataTimestamp(self, parent_id, metadataPath):
        with self.connect(commit=True) as cu:
            stmt = 'insert into metadata_timestamps(path_id, mtime) values(?, ?)'
            cu.execute(stmt, (parent_id, os.stat(metadataPath).st_mtime))
            
    _tableNameFromType = {
        # Put anomalies here.
    }
    def tableNameFromType(self, itemType):
        return self._tableNameFromType.get(itemType, itemType)
                
    def getCommandInfo(self, path_id, cu=None):
        obj = {}
        names = self._specific_names['command']
        with self.connect(cu=cu) as cu:
            self.getCommonToolDetails(path_id, obj, cu)
            cu.execute(("select %s from command where path_id = ?" %
                        ", ".join(names)), (path_id,))
            row = cu.fetchone()
        for i, name in enumerate(names):
            obj[name] = row[i]
        return obj
    
    def getCommonContainerDetails(self, path_id, tableName):
        obj = {}
        names = self._specific_names[tableName]
        with self.connect() as cu:
            cu.execute("""select name from common_details
                          where path_id = ?""", (path_id,))
            row = cu.fetchone()
            if row:
                obj['name'] = row[0]
            cu.execute(("select %s from %s where path_id = ?" %
                        (", ".join(names), tableName)), (path_id,))
            row = cu.fetchone()
        if row:
            for i, name in enumerate(names):
                obj[name] = row[i]
        return obj

    def getCommonToolDetails(self, path_id, obj, cu=None):
        with self.connect(cu=cu) as cu:
            cu.execute("""select name from common_details
                          where path_id = ?""", (path_id,))
            row = cu.fetchone()
            if row is None:
                raise Exception("koToolbox2.py:getCommonToolDetails internal error: path_id:%d, table:common_details => None" % (path_id,))
            obj['name'] = row[0]
            cu.execute("""select value, keyboard_shortcut from common_tool_details
                          where path_id = ?""", (path_id,))
            row = cu.fetchone()
            obj['value'] = row[0]
            obj['keyboard_shortcut'] = row[1]
            cu.execute("""select prop_name, prop_value from misc_properties
                          where path_id = ?""", (path_id,))
            rows = cu.fetchall()
            for row in rows:
                obj[row[0]] = row[1]

    def getSimpleToolInfo(self, path_id, cu=None):
        obj = {}
        with self.connect() as cu:
            self.getCommonToolDetails(path_id, obj, cu)
        obj['url'] = obj['value']
        # Komodo accesses the value by as a URL so it can
        # get a koFileEx obj
        return obj
    
    def getAbbreviationSnippetId(self, abbrev, subnames):
        """
        Return the first snippet whose name matches 'abbrev' from
        a folder consisting of Abbreviations/[s in subnames],
        ignoring case for the subnames.

        This query uses self-joins so it can be done in one shot
        to the database.
        
        Without the question-marks:
        
        select cd3.path_id, cd3.name as cd3name, cd2.name
                from hierarchy as h2, hierarchy as h1,
                     common_details as cd3, common_details as cd2, common_details as cd1
                where cd1.name = "Abbreviations" and cd1.type = "folder"
                  and h1.parent_path_id = cd1.path_id
                  and h2.parent_path_id = h1.path_id
                  and h2.parent_path_id = cd2.path_id and cd2.type = "folder" 
                  and (cd2.name like subnames[0] or cd2.name like subnames[1]...)
                  and cd3.path_id = h2.path_id and cd3.type = "snippet" and cd3.name like <abbrev>

        In Sqlite, doing a 'like' on a value ignores case.  Since abbrev
        need to be names, we don't need to worry about wildcard chars.
        """
        #log.debug("Look for abbrev %s in subnames %s", abbrev, subnames)
        condition_part = ['cd2.name like ?'] * len(subnames)
        condition = ' or '.join(condition_part)
        stmt = """select cd3.path_id, cd3.name as cd3name, cd2.name
                from hierarchy as h2, hierarchy as h1,
                     common_details as cd3, common_details as cd2, common_details as cd1
                where cd1.name = ? and cd1.type = ?
                  and h1.parent_path_id = cd1.path_id
                  and h2.parent_path_id = h1.path_id
                  and h2.parent_path_id = cd2.path_id
                  and cd2.type = ? and (%s)
                  and cd3.path_id = h2.path_id and cd3.type = ? and cd3.name like ?
                  """ % condition
        test_values = ["Abbreviations", "folder", "folder"] + subnames + ["snippet", abbrev]
        #log.debug("stmt:%s, test_values:%s", stmt, test_values)
        with self.connect() as cu:
            cu.execute(stmt, test_values)
            matches = cu.fetchall()
        if len(matches) == 0:
            #log.debug("no matches")
            return None
        elif len(matches) == 1:
            #log.debug("exactly 1 match: %s", matches[0][0])
            return matches[0][0]
        # Favor case-sensitive matches over non-general-folder matches.
        exactMatches = [x for x in matches if x[1] == abbrev]
        if len(exactMatches) == 1:
            #log.debug("exactly 1 case-sensitive match: %d", exactMatches[0][0])
            return exactMatches[0][0]
        #log.debug("Got %d matches, returning %d (%s)", len(matches), matches[0][0], matches[0][1])
        # Favor snippets that aren't in a General folder.
        nonGeneralMatches = [x for x in matches if x[2] != "General"]
        if nonGeneralMatches:
            #log.debug("Got %d nonGeneralMatches, returning %d (%s)", len(nonGeneralMatches), nonGeneralMatches[0][0], nonGeneralMatches[0][1])
            return nonGeneralMatches[0][0]
        return matches[0][0]

    def getIDsByType(self, nodeType):
        with self.connect() as cu:
            stmt = 'select path_id from %s' % nodeType
            cu.execute(stmt)
            res = cu.fetchall()
            if res is None: return []
        return [x[0] for x in res]
        
    def getIDsForToolsWithKeyboardShortcuts(self):
        with self.connect() as cu:
            stmt = 'select path_id from common_tool_details where keyboard_shortcut != ?'
            cu.execute(stmt, ("",))
            res = cu.fetchall()
            if res is None: return []
        ids = [x[0] for x in res]
        return ids

    def getTriggerMacroIDs(self):
        with self.connect() as cu:
            stmt = 'select path_id from macro where trigger_enabled = ? and trigger != ?'
            cu.execute(stmt, (True, ""))
            res = cu.fetchall()
            if res is None: return []
        ids = [x[0] for x in res]
        return ids        

    def getMacroInfo(self, path_id, cu=None):
        obj = {}
        names = self._specific_names['macro']
        with self.connect(cu=cu) as cu:
            self.getCommonToolDetails(path_id, obj, cu)
            cu.execute(("select %s from macro where path_id = ?" %
                        ", ".join(names)), (path_id,))
            row = cu.fetchone()
        for i, name in enumerate(names):
            try:
                obj[name] = row[i]
            except TypeError:
                log.error("couldn't get prop %s for macro id %r", name, i)
        return obj

    def deleteItem(self, path_id, cu=None):
        with self.connect(commit=True, cu=cu) as cu:
            stmt = "select path_id from hierarchy where parent_path_id = ?"
            cu.execute(stmt, (path_id,))
            children = cu.fetchall()
            for child_id in children:
                self.deleteItem(child_id[0], cu)
            path = self.getPath(path_id, cu)
            tableNames = ['common_details',
                          'common_tool_details',
                          'misc_properties',
                          'hierarchy',
                          'favorites']
            res = self.getValuesFromTableByKey('common_details', ['type'],
                                               'path_id', path_id, cu)
            if res:
                tool_type = res[0]
                if tool_type in ['snippet', 'macro', 'command', 'menu', 'toolbar']:
                    tableNames.append(tool_type)
            for t in tableNames:
                try:
                    cu.execute("DELETE FROM %s WHERE path_id=?" % t, (path_id,))
                except:
                    log.debug("Can't delete from table %s", t)
            cu.execute("DELETE FROM paths WHERE id=?", (path_id,))
        
    def saveToolName(self, path_id, name, old_name=None):
        with self.connect(commit=True) as cu:
            if old_name is None:
                old_name = self.getValuesFromTableByKey('common_details', ['name'],
                                                        'path_id', path_id, cu)[0]
            if name != old_name:
                self.updateValuesInTableByKey('common_details', ['name'], [name],
                                              ['path_id'], [path_id], cu)

    def saveContent(self, path_id, value):
        with self.connect(commit=True) as cu:
            self.updateValuesInTableByKey('common_tool_details',
                                          ['value'], [value],
                                          ['path_id'], [path_id], cu)
        
    def save_commonToolDetails(self, path_id, oldMacroInfo, attributes, new_value, cu=None):
        # Only update the changed values.
        # Reading from DB is cheaper than writing to it.
        old_value = oldMacroInfo['value']
        old_kbsc = oldMacroInfo['keyboard_shortcut']
        new_kbsc = attributes.get('keyboard_shortcut', "")
        if old_value != new_value or old_kbsc != new_kbsc:
            field_names = []
            field_values = []
            if old_value != new_value:
                field_names.append('value')
                field_values.append(new_value)
            if old_kbsc != new_kbsc:
                field_names.append('keyboard_shortcut')
                field_values.append(new_kbsc)
            with self.connect(commit=True, cu=cu) as cu:
                self.updateValuesInTableByKey('common_tool_details',
                                              field_names, field_values,
                                              ['path_id'], [path_id], cu)
                

    def _removeNonMiscAttributeNames(self, oldMacroInfo, work_attributes):
        for name in ['name', 'path', 'value', 'keyboard_shortcut']:
            try: del work_attributes[name]
            except KeyError:
                pass
            try: del oldMacroInfo[name]
            except KeyError:
                pass
    
    def _saveNamedValuesInTable(self, path_id, table_name, names, old_attributes, new_attributes, cu):
        names_to_update = []
        vals_to_update = []
        for name in names:
            if name in new_attributes:
                if old_attributes[name] != new_attributes[name]:
                    names_to_update.append(name)
                    vals_to_update.append(new_attributes[name])
                del new_attributes[name]
                del old_attributes[name]
        if names_to_update:
            self.updateValuesInTableByKey(table_name,
                                          names_to_update, vals_to_update,
                                          ['path_id'], [path_id], cu)

    def saveToolInfo(self, path_id, table_name, tool_name, value, attributes,
                     info_getter):
        work_attributes = attributes.copy()
        with self.connect(commit=True) as cu:
            oldInfo = info_getter(path_id, cu)
            self.saveToolName(path_id, tool_name, oldInfo['name'])
            self.save_commonToolDetails(path_id, oldInfo, attributes, value, cu)
            specific_names = self._specific_names.get(table_name)
            if specific_names:
                self._saveNamedValuesInTable(path_id, table_name, specific_names,
                                             oldInfo, work_attributes, cu)
            self._removeNonMiscAttributeNames(oldInfo, work_attributes)
            self.saveMiscInfo(path_id, oldInfo, work_attributes, cu)

    def saveContainerInfo(self, path_id, table_name, tool_name, attributes,
                          info_getter):
        work_attributes = attributes.copy()
        with self.connect(commit=True) as cu:
            oldInfo = info_getter(path_id, cu)
            self.saveToolName(path_id, tool_name, oldInfo['name'])
            specific_names = self._specific_names.get(table_name, None)
            if specific_names:
                self._saveNamedValuesInTable(path_id, table_name, specific_names,
                                             oldInfo, work_attributes, cu)

    def saveCommandInfo(self, path_id, name, value, attributes):
        self.saveToolInfo(path_id, 'command', name, value, attributes,
                          self.getCommandInfo)

    def saveSimpleToolInfo(self, path_id, name, value, attributes):
        self.saveToolInfo(path_id, None, name, value, attributes,
                          None, self.getSimpleToolInfo)

    def saveMacroInfo(self, path_id, name, value, attributes):
        self.saveToolInfo(path_id, 'macro', name, value, attributes,
                          self.getMacroInfo)
    
    def saveSnippetInfo(self, path_id, name, value, attributes):
        self.saveToolInfo(path_id, 'snippet', name, value, attributes,
                          self.getSnippetInfo)
    
    def saveMenuInfo(self, path_id, tool_name, attributes):
        self.saveContainerInfo(path_id, 'menu', tool_name, attributes,
                               self.getMenuInfo)
    
    def saveToolbarInfo(self, path_id, tool_name, attributes):
        self.saveContainerInfo(path_id, 'toolbar', tool_name, attributes,
                               self.getToolbarInfo)
            
    def saveMiscInfo(self, path_id, oldAttrList, newAttrList, cu=None):
        names_to_update = []
        vals_to_update = []
        names_to_insert = []
        vals_to_insert = []
        names_to_delete = []
        new_names = newAttrList.keys()
        old_names = oldAttrList.keys()
        for name in new_names:
            if name in old_names:
                if newAttrList[name] != oldAttrList[name]:
                    names_to_update.append(name)
                    vals_to_update.append(newAttrList[name])
                del oldAttrList[name]
            else:
                names_to_insert.append(name)
                vals_to_insert.append(newAttrList[name])
            del newAttrList[name]
        names_to_delete += oldAttrList.keys()
            
        log.debug("misc updates: names:%s, values:%s", names_to_update, vals_to_update)
        log.debug("misc inserts: names:%s, values:%s", names_to_insert, vals_to_insert)
        log.debug("misc deletions: names:%s", names_to_delete)
        with self.connect(commit=True, cu=cu) as cu:
            for name, val in zip(names_to_update, vals_to_update):
                self.updateValuesInTableByKey('misc_properties',
                                              ['prop_value'], [val],
                                              ['path_id', 'prop_name'],
                                              [path_id, name], cu)
            for name, val in zip(names_to_insert, vals_to_insert):
                self.insertRowInTable('misc_properties',
                                      ['path_id', 'prop_name', 'prop_value'],
                                      [path_id, name, val],
                                      cu)
            for name in names_to_delete:
                self.deleteRowByKey('misc_properties',
                                    ['path_id', 'prop_name'],
                                    [path_id, name], cu)
                
    def getSnippetInfo(self, path_id, cu=None):
        obj = {}
        names = self._specific_names.get('snippet')
        with self.connect() as cu:
            self.getCommonToolDetails(path_id, obj, cu)
            cu.execute(("select %s from snippet where path_id = ?" %
                        ", ".join(names)), (path_id,))
            row = cu.fetchone()
        for i, name in enumerate(names):
            obj[name] = row[i]
        return obj
    
    def getMenuInfo(self, path_id, cu=None):
        return self.getCommonContainerDetails(path_id, 'menu')
    
    def getToolbarInfo(self, path_id, cu=None):
        return self.getCommonContainerDetails(path_id, 'toolbar')

class ToolboxLoader(object):
    # Pure Python class that manages the new Komodo Toolbox back-end

    koToolExt = ".kotool"

    def __init__(self, db_path, schemaFile):
        # This timestamp is only used while loading the database.
        try:
            self.dbTimestamp = os.stat(db_path).st_mtime
        except:
            self.dbTimestamp = 0
        self.db = Database(db_path, schemaFile)
        
    def deleteFolderIfMetadataChanged(self, path, fname, path_id):
        # fname is last part of path, but is in for convenience
        # path_id is id(path)
        # returns True if the item should be re-added to the database
        metadataPath = join(path, UI_FOLDER_FILENAME)
        try:
            st_res = os.stat(metadataPath)
            mtime_now = st_res.st_mtime
            has_metadata_now = True
        except:
            has_metadata_now = False
        result = self.db.getValuesFromTableByKey('metadata_timestamps', ['mtime'], 'path_id', path_id)
        if result:
            has_metadata_then = True
            mtime_then = result[0]
        else:
            has_metadata_then = False
        if has_metadata_then != has_metadata_now:
            update_tree = True
        elif not has_metadata_then:
            update_tree = False
        else:
            update_tree = mtime_now > mtime_then
        if update_tree:
            self.db.deleteItem(path_id)
        return update_tree

    def walkFunc(self, arg, dirname, fnames):
        parent_id = self.db.get_id_from_path(dirname)
        if self.dbTimestamp:
            existing_child_ids = dict([(x, 1) for x in self.db.getChildIDs(parent_id)])
        #TODO: Delete any unhandled IDs at end
        for fname in fnames:
            path = join(dirname, fname)
            isDir = os.path.isdir(path)
            isTool = os.path.splitext(fname)[1] == TOOL_EXTENSION
            if not isDir and not isTool:
                continue
            elif not self.dbTimestamp:
                need_update = True
            else:
                result_list = self.db.getValuesFromTableByKey('paths',
                                                              ['id'],
                                                              'path', path)
                if result_list:
                    id = result_list[0]
                    try: del existing_child_ids[id]
                    except KeyError: pass
                    if id is None:
                        need_update = True
                    elif isDir:
                        need_update = self.deleteFolderIfMetadataChanged(path, fname, id)
                    else:
                        mtime = os.stat(path).st_mtime
                        need_update = mtime > self.dbTimestamp
                        if need_update:
                            log.debug("db time: %r, stat time: %r", self.dbTimestamp, mtime)
                            log.debug("Rebuilding item %s (%s)", fname, dirname)
                            self.db.deleteItem(id)
                else:
                    need_update = True
                    
            if need_update:
                if isTool:
                    try:
                        fp = open(path, 'r')
                    except IOError:
                        log.error("database loader: Couldn't load file %s", path)
                        continue
                    data = json.load(fp, encoding="utf-8")
                    fp.close()
                    type = data['type']
                    self.db.addTool(data, type, path, fname, parent_id)
                else:
                    self.db.addFolder(path, fname, parent_id)
        metadataPath = join(dirname, UI_FOLDER_FILENAME)
        if exists(metadataPath):
            result = self.db.getValuesFromTableByKey('metadata_timestamps', ['mtime'], 'path_id', parent_id)
            if result is None:
                # If it isn't None, it means it's up to date, and there's nothing to do.
                # Add the children here
                fp = open(metadataPath, 'r')
                try:
                    data = json.load(fp, encoding="utf-8")
                except ValueError:
                    log.error("Couldn't process json file %s", metadataPath)
                    data = {}
                fp.close()
                children = data.get('children', [])
                position = 0
                self.db.insertMetadataTimestamp(parent_id, metadataPath)
                for child_name in children:
                    if 'ci2' in child_name:
                        print "Stop here"
                    child_path = join(dirname, self._slugify(child_name) + TOOL_EXTENSION)
                    child_id = self.db.get_id_from_path(child_path)
                    if child_id is None:
                        complain = True
                        try:
                            fp = open(child_path, 'r')
                            try: 
                                data2 = json.load(fp, encoding="utf-8")
                            finally:
                                fp.close()
                            complain = data2['type'] != 'file'
                        except:
                            pass
                        if complain:
                            log.error("Didn't find an ID for path %s", child_path)
                    else:
                        self.db.insertMenuItem(child_id, position)
                        position += 1
    
    _slugify_re = re.compile(r'[^a-zA-Z0-9\-=\+]+')        
    def _slugify(self, s):
        return re.sub(self._slugify_re, '_', s)

    def loadToolboxDirectory(self, toolboxName, toolboxDir):
        if toolboxDir.endswith(DEFAULT_TARGET_DIRECTORY):
            actualToolboxDir = toolboxDir
        else:
            actualToolboxDir = join(toolboxDir, DEFAULT_TARGET_DIRECTORY)
        if not exists(actualToolboxDir):
            log.error("No toolbox subdirectory %s at %s", DEFAULT_TARGET_DIRECTORY, toolboxDir)
            return
        self.db.establishConnection()
        self._loadedPaths[actualToolboxDir] = True
        log.debug("Reading dir %s", toolboxDir)
        try:
            result_list = self.db.getValuesFromTableByKey('paths',
                                                   ['id'],
                                                   'path', actualToolboxDir)
            if not result_list:
                self.db.addFolder(actualToolboxDir, toolboxName, None)
                result_list = self.db.getValuesFromTableByKey('paths',
                                                              ['id'],
                                                              'path', actualToolboxDir)
            os.path.walk(actualToolboxDir, self.walkFunc, None)
        finally:
            self.db.releaseConnection()
        return result_list[0]
        
    def markAllTopLevelItemsUnloaded(self):
        self._loadedPaths = dict([(x, False) for x in self.db.getTopLevelPaths()])
        print "" #debug
        
    def deleteUnloadedTopLevelItems(self):
        for path, isLoaded in self._loadedPaths.items():
            if not isLoaded:
                id = self.db.get_id_from_path(path)
                if id is not None:
                    log.debug("We never loaded path %s (id %d)", path, id)
                    self.db.deleteItem(id)
            
# Use this class for testing only.
class ToolboxAccessor(object):
    def __init__(self, db_path):
        self.db = Database(db_path)

    #def getTopLevelNodes(self):
    #    return self.db.getTopLevelNodes()
        
    def __getattr__(self, name):
        return getattr(self.db, name)

def main(argv):
    #dbFile = r"c:\Users\ericp\trash\menu-test.sqlite"
    #schemaFile = r"c:\Users\ericp\svn\apps\komodo\src\projects\koToolbox.sql"
    #toolboxLoader = ToolboxLoader(dbFile, schemaFile)
    #toolboxLoader.loadToolboxDirectory(r"c:\Users\ericp\trash\testToolbox")
    #toolboxAccessor = ToolboxAccessor(dbFile)
    #print toolboxAccessor.getTopLevelNodes()
    #return
    #
    dbFile = r"c:\Users\ericp\trash\toolbox-test.sqlite"
    schemaFile = r"c:\Users\ericp\svn\apps\komodo\src\toolbox\koToolbox.sql"
    try:
        toolboxLoader = ToolboxLoader(dbFile, schemaFile)
        toolboxLoader.markAllTopLevelItemsUnloaded()
        import time
        t1 = time.time()
        toolboxLoader.loadToolboxDirectory(r"c:\Users\ericp\trash\stdToolbox")
        t2 = time.time()
        log.debug("Time to load std-toolbox: %g msec", (t2 - t1) * 1000.0)
        t1 = time.time()
        toolboxLoader.loadToolboxDirectory(r"c:\Users\ericp\trash\sharedToolbox")
        t2 = time.time()
        log.debug("Time to load shared-toolbox: %g msec", (t2 - t1) * 1000.0)
        toolboxLoader.deleteUnloadedTopLevelItems()
        toolboxAccessor = ToolboxAccessor(dbFile)
        print toolboxAccessor.getTopLevelNodes()
        print "Wahoo!!!"
    except:
        log.exception("Failure")
        os.unlink(dbFile)


"""
Notes on feeding the tree via the db:

class koToolbox2Node(object):
  node_id
  node_name
  node_type
  is_open: init False
  level
  child_nodes: init None, set to array of [node_id]
  

nsITreeView is trivial...

rowCount: len(self._rows) // no db
getCellProperties:(row, col, properties):
  get atom for self._rows[row].node_type
isContainer(index):
  return self._isContainerType(self._rows[index].node_type)
isContainerOpen(index):
  return self.isContainerType(self._rows[index].is_open)
isContainerEmpty(index):
  if not self.isContainer(index): return
  if self._rows[index].child_nodes is None:
     self._rows[index].child_nodes = db.getChildNodes(self._rows[index].node_id)
  return not self._rows[index].child_nodes
getParentIndex: std
hasNextSibling: std
getCellText(row, col):
  return self._rows[row].node_name
toggleOpenState(index):
  node = self._rows[index]
  if node.is_open:
    delete self._rows[index + 1: ...], updateRowCount
  else:
    update node.child_nodes
    self._sort(node.child_nodes)
    insert node.child_nodes into self._rows[index], filtering

Modern view:
Uses the following values:
* isFavorite
* # times used
* lastUsed
* context -- fileType, extensions
* tags in filter

"""

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
