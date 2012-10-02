/* ***** BEGIN LICENSE BLOCK *****
 * Version: MPL 1.1/GPL 2.0/LGPL 2.1
 * 
 * The contents of this file are subject to the Mozilla Public License
 * Version 1.1 (the "License"); you may not use this file except in
 * compliance with the License. You may obtain a copy of the License at
 * http://www.mozilla.org/MPL/
 * 
 * Software distributed under the License is distributed on an "AS IS"
 * basis, WITHOUT WARRANTY OF ANY KIND, either express or implied. See the
 * License for the specific language governing rights and limitations
 * under the License.
 * 
 * The Original Code is Komodo code.
 * 
 * The Initial Developer of the Original Code is ActiveState Software Inc.
 * Portions created by ActiveState Software Inc are Copyright (C) 2000-2007
 * ActiveState Software Inc. All Rights Reserved.
 * 
 * Contributor(s):
 *   ActiveState Software Inc
 * 
 * Alternatively, the contents of this file may be used under the terms of
 * either the GNU General Public License Version 2 or later (the "GPL"), or
 * the GNU Lesser General Public License Version 2.1 or later (the "LGPL"),
 * in which case the provisions of the GPL or the LGPL are applicable instead
 * of those above. If you wish to allow use of your version of this file only
 * under the terms of either the GPL or the LGPL, and not to allow others to
 * use your version of this file under the terms of the MPL, indicate your
 * decision by deleting the provisions above and replace them with the notice
 * and other provisions required by the GPL or the LGPL. If you do not delete
 * the provisions above, a recipient may use your version of this file under
 * the terms of any one of the MPL, the GPL or the LGPL.
 * 
 * ***** END LICENSE BLOCK ***** */
// adapted from browser/components/nsBrowserContentHandler.js

const winOptions = 
// #if PLATFORM == "darwin"
  "chrome,resizable=yes,menubar,toolbar,status,all,dialog=no";
// #else
  "chrome,resizable=yes,menubar,toolbar,status,all";
// #endif

const Cc = Components.classes;
const Ci = Components.interfaces;
Components.utils.import("resource://gre/modules/XPCOMUtils.jsm");

function shouldLoadURI(aURI) {
  if (aURI && !aURI.schemeIs("chrome"))
    return true;
	
  //log.warn("*** Preventing external load of chrome: URI into window\n");
  //log.warn("    Use -chrome <uri> instead\n");
  return false;
}

function resolveURIInternal(aCmdLine, aArgument) {
  var uri = aCmdLine.resolveURI(aArgument);

  if (!(uri instanceof Ci.nsIFileURL)) {
    return uri;
  }

  try {
    if (uri.file.exists())
      return uri;
  }
  catch (e) {
    Components.utils.reportError(e);
  }

  // We have interpreted the argument as a relative file URI, but the file
  // doesn't exist. Try URI fixup heuristics: see bug 290782.
 
  try {
    var urifixup = Cc["@mozilla.org/docshell/urifixup;1"]
                             .getService(Ci.nsIURIFixup);

    uri = urifixup.createFixupURI(aArgument, 0);
  }
  catch (e) {
    Components.utils.reportError(e);
  }

  return uri;
}

function openWindow(parent, url, target, features, args) {
    var wwatch = Cc["@mozilla.org/embedcomp/window-watcher;1"]
            .getService(Ci.nsIWindowWatcher);
    return wwatch.openWindow(parent, url, target, features, args);
}

// Duplicate of windowManager.js:windowManager_getMainWindow.
function getMostRecentWindow(aType) {
    var wm = Cc["@mozilla.org/appshell/window-mediator;1"]
            .getService(Ci.nsIWindowMediator);
    return wm.getMostRecentWindow(aType);
}

/* A modified copy of dialogs.js::dialog_internalError() to make
 * window launching work here.
 */
function _internalError(error, text)
{
    if (typeof(error) == 'undefined' || error == null)
        throw("Must specify 'error' argument to _internalError().");
    if (typeof(text) == 'undefined' || text == null)
        throw("Must specify 'text' argument to _internalError().");

    // Show the dialog.
    var args =  Cc["@mozilla.org/supports-array;1"]
           .createInstance(Ci.nsISupportsArray);
    var errorObj = Cc["@mozilla.org/supports-string;1"]
           .createInstance(Ci.nsISupportsString);
    errorObj.data = error;
    args.AppendElement(errorObj);
    var textObj = Cc["@mozilla.org/supports-string;1"]
            .createInstance(Ci.nsISupportsString);
    textObj.data = text;
    args.AppendElement(textObj);
    openWindow(null,
               "chrome://komodo/content/dialogs/internalError.xul",
               "_blank",
               "chrome,modal,titlebar",
               args);
}

function komodoCmdLineHandler() { }
komodoCmdLineHandler.prototype = {
  chromeURL : "chrome://komodo/content",

  /* nsICommandLineHandler */
  handle : function dch_handle(cmdLine) {
    var urilist = [];
    try {
      var ar;
      while ((ar = cmdLine.handleFlagWithParam("url", false))) {
        urilist.push(resolveURIInternal(cmdLine, ar));
      }
    }
    catch (e) {
      Components.utils.reportError(e);
    }

    var count = cmdLine.length;

    for (var i = 0; i < count; ++i) {
      var curarg = cmdLine.getArgument(i);
      if (curarg == "-file") {
	// Mac OS X passes this flag before the filename arguument when using
	// "open -a Komodo.app somefile.txt", see bug 86470. We just ignore this
	// flag and the filename will be opened in the next iteration.
	continue;
      } else if (curarg.match(/^-/)) {
        Components.utils.reportError("Warning: unrecognized command line flag " + curarg + "\n");
        // To emulate the pre-nsICommandLine behavior, we ignore
        // the argument after an unrecognized flag.
        ++i;
      } else {
        try {
          urilist.push(resolveURIInternal(cmdLine, curarg));
        }
        catch (e) {
          Components.utils.reportError("Error opening URI '" + curarg + "' from the command line: " + e + "\n");
        }
      }
    }

    var koWin = getMostRecentWindow("Komodo");
    if (urilist.length) {
      var obsvc = Cc["@mozilla.org/observer-service;1"].
            getService(Ci.nsIObserverService);
      var speclist = [];
      for (var uri in urilist) {
        if (shouldLoadURI(urilist[uri])) {
          // Ensure the URI is decoded, bug 72873.
          speclist.push(decodeURI(urilist[uri].spec));
        }
      }
      if (speclist.length) {
        if (speclist.length == 1) {
          speclist = speclist[0];
        } else {
          speclist = speclist.join("|");
        }
        if (!cmdLine.preventDefault && !koWin) {
          // if we couldn't load it in an existing window, open a new one
          var args =  Cc["@mozilla.org/supports-array;1"]
                 .createInstance(Ci.nsISupportsArray);
  
          var paramBlock = 
              Cc["@mozilla.org/embedcomp/dialogparam;1"].
              createInstance(Ci.nsIDialogParamBlock);
          paramBlock.SetString(0, speclist);
          args.AppendElement(paramBlock);

          openWindow(null, this.chromeURL, "_blank", winOptions, args);
          cmdLine.preventDefault = true; // stop the browser from handling this also
          return;
        }
        try {
            obsvc.notifyObservers(this, 'open-url', speclist);
        } catch(e) { /* exception if no listeners */ }
      }

    }
    else if (!cmdLine.preventDefault && !koWin) {
      openWindow(null, this.chromeURL, "_blank", winOptions);
      cmdLine.preventDefault = true; // stop the browser from handling this also
    }
  },

  // XXX localize me... how?
  helpInfo : "Usage: komodo [-flags] [<url>]\n",

  classDescription: "komodoCmdLineHandler",
  classID: Components.ID("{07DCEAC7-31F6-11DA-BC61-000D935D3368}"),
  contractID: "@activestate.com/komodo/final-clh;1",
  QueryInterface: XPCOMUtils.generateQI([Ci.nsICommandLineHandler]),
  _xpcom_categories: [{category: "command-line-handler", entry: "m-komodo"}]
};

if ("generateNSGetFactory" in XPCOMUtils) {
    var NSGetFactory = XPCOMUtils.generateNSGetFactory([komodoCmdLineHandler]);
} else if ("generateNSGetModule" in XPCOMUtils) {
    var NSGetModule = XPCOMUtils.generateNSGetModule([komodoCmdLineHandler]);
}
