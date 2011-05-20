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
# Portions created by ActiveState Software Inc are Copyright (C) 2000-2011
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

"""Test some Node.js-specific codeintel handling."""

import os
import sys
import re
from os.path import join, dirname, abspath, exists, basename
from glob import glob
import unittest
import subprocess
import logging

from codeintel2.common import *
from codeintel2.util import indent, dedent, banner, markup_text, unmark_text
from codeintel2.environment import SimplePrefsEnvironment

from testlib import TestError, TestSkipped, TestFailed, tag
from citestsupport import CodeIntelTestCase, run, writefile



log = logging.getLogger("test")

class CplnTestCase(CodeIntelTestCase):
    lang = "Node.js"
    test_dir = join(os.getcwd(), "tmp")

    @tag("nodejs")
    def test_nodejs_require(self):
        test_dir = join(self.test_dir, "test_nodejs_require")
        content, positions = unmark_text(dedent("""\
            var http = require('http');
            var fs = require('fs');
            http.<1>;
            fs.<2>;
        """))
        manifest = [
            ("http.js", dedent("""
                /* as generated by node_html_to_js.py */
                var http_ = {};
                http_.Server = function Server() {}
                http_.Server.prototype = {}
                /**
                 * Start a UNIX socket server listening for connections on the given path.
                 */
                http_.Server.prototype.listen = function() {}
                exports = http_;
             """)),
            ("fs.js", dedent("""
                /* possible alternative for manually written files */
                exports = {
                    rename: function() {}
                }
            """)),
            ("foo.js", content),
        ]
        for file, content in manifest:
            path = join(test_dir, file)
            writefile(path, content)
        buf = self.mgr.buf_from_path(join(test_dir, "foo.js"))
        self.assertCompletionsInclude2(buf, positions[1],
            [("class", "Server"), ])
        self.assertCompletionsInclude2(buf, positions[2],
            [("function", "rename"), ])

    @tag("nodejs")
    def test_nodejs_require_nonvar(self):
        """
        Test require() without intermediate assignment
        """
        test_dir = join(self.test_dir, "test_nodejs_require")
        content, positions = unmark_text(dedent("""\
            require('dummy').<1>;
        """))
        manifest = [
            ("dummy.js", dedent("""
             exports = {
               method: function() {}
             }
             """)),
            ("foo.js", content),
        ]
        for file, content in manifest:
            path = join(test_dir, file)
            writefile(path, content)
        buf = self.mgr.buf_from_path(join(test_dir, "foo.js"))
        self.assertCompletionsInclude2(buf, positions[1],
            [("function", "method"), ])

#---- mainline

if __name__ == "__main__":
    unittest.main()
