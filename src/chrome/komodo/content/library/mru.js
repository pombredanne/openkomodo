/* Copyright (c) 2000-2006 ActiveState Software Inc.
   See the file LICENSE.txt for licensing information. */

// The New Plan:
//    - This module is reponsible for MRUs (examples: recent files, recent
//      projects, recent find/replace patterns).
//    - It has the smarts to tie an MRU to a koIOrderedPreference so that
//      it is serialized.
//    - It has the smarts to observe changes in relevant prefs which affect
//      the MRUs, e.g. mruProjectSize.
//      XXX IMO, This is the wrong place to do this. Ideally there would be a
//          koIMRU interface with separate implementations for the various
//          MRUs (and possibly subclassed interfaces for special behaviour).
//    - As required, other modules will call this module's MRU_* methods
//      to note changes in particular MRUs.
//    - This module will send out a "mru_changed" notification whenever an
//      MRU changes. It is up to the various UIs (e.g. File->Recent Files,
//      Getting Started) to observe this notification and act accordingly.
//

//---- globals
if (typeof(ko)=='undefined') {
    var ko = {};
}

ko.mru = {};
(function() {
    
var gMRUPrefObserver = null;
var _log = ko.logging.getLogger('ko.mru');
//_log.setLevel(ko.logging.LOG_DEBUG);


//---- internal support routines

function _notifyOfMRUChange(prefName)
{
    try {
        var observerSvc = Components.classes["@mozilla.org/observer-service;1"].
                    getService(Components.interfaces.nsIObserverService);
        observerSvc.notifyObservers(null, 'mru_changed', prefName);
    } catch(e) {
        // Raises exception if no observers are registered. Just ignore that.
    }
}


/* Observe changes to MRUs *size* prefs matching the name "mru*Size" and trim
 * the associated "mru*List" pref if necessary.
 */
function _MRUPrefObserver()
{
    this.prefSvc = Components.classes["@activestate.com/koPrefService;1"].
                  getService(Components.interfaces.koIPrefService);
    this.prefSvc.prefs.addObserver(this);
};
_MRUPrefObserver.prototype.destroy = function()
{
    this.prefSvc = Components.classes["@activestate.com/koPrefService;1"].
                  getService(Components.interfaces.koIPrefService);
    this.prefSvc.prefs.removeObserver(this);
    this.prefSvc = null;
}
_MRUPrefObserver.prototype.observe = function(prefSet, prefSetID, sizePrefName)
{
    // Adjust the size of MRUs if the size preference for that MRU changes.
    if (sizePrefName.slice(0, 3) != "mru"
        || sizePrefName.slice(-4) != "Size")
    {
        return;
    }
    if (!this.prefSvc.prefs.hasPref(sizePrefName)) {
        return;
    }

    _log.info("observed pref '" + sizePrefName + "' change (prefSet=" +
                prefSet + ", prefSetID=" + prefSetID + ")");
    var listPrefName = sizePrefName.replace("Size", "List");
    var mruList = null;
    if (this.prefSvc.prefs.hasPref(listPrefName)) {
        mruList = this.prefSvc.prefs.getPref(listPrefName);
    }
    if (mruList) {
        // Chop the list to the correct size.
        var maxEntries = MRU_maxEntries(listPrefName);
        while (mruList.length > maxEntries) {
            mruList.deletePref(mruList.length - 1);
        }
        _notifyOfMRUChange(listPrefName);
    }
};


//---- public methods

this.initialize = function MRU_initialize()
{
    _log.info("initialize()");
    gMRUPrefObserver = new _MRUPrefObserver();
    ko.main.addUnloadHandler(ko.mru.finalize);
}


this.finalize = function MRU_finalize()
{
    _log.info("finalize()");
    gMRUPrefObserver.destroy();
    gMRUPrefObserver = null;
}


this.maxEntries = function MRU_maxEntries(listPrefName)
{
    _log.info("maxEntries(listPrefName="+listPrefName+")");
    // XXX:HACK This should be an attribute on difference instances of a
    //          koIMRU interface.
    var maxEntries;
    var prefSvc = Components.classes["@activestate.com/koPrefService;1"].
                  getService(Components.interfaces.koIPrefService);
    var sizePrefName = null;
    if (listPrefName.slice(0, 3) == "mru"
        && listPrefName.slice(-4) == "List")
    {
        sizePrefName = listPrefName.replace("List", "Size");
    }
    if (sizePrefName && prefSvc.prefs.hasPref(sizePrefName))  {
        maxEntries = prefSvc.prefs.getLongPref(sizePrefName);
    } else {
        maxEntries = 50; // Bigger?
    }
    return maxEntries;
}


this.add = function MRU_add(prefName, entry, caseSensitive)
{
    _log.info("MRU_add(prefName="+prefName+", entry="+entry+
                ", caseSensitive="+caseSensitive+")");

    // Add the given "entry" (a string) to the given MRU (indentified by
    // the name of pref, "prefName", with which it is associated).
    // "caseSensitive" is a boolean indicating how to match "entry" with
    // existing MRU entries.
    var prefSvc = Components.classes["@activestate.com/koPrefService;1"].
                  getService(Components.interfaces.koIPrefService);

    // Validate arguments.
    var errmsg;
    if (!prefName) {
        errmsg = "MRU_add: invalid argument: prefName='"+prefName+"'";
        _log.error(errmsg);
        throw(errmsg);
    }
    if (!entry) {
        errmsg = "MRU_add: warning: no entry: prefName='"+prefName+
                 "', entry='"+entry+"'";
        _log.warn(errmsg)
    }

    var mruList = null;
    if (prefSvc.prefs.hasPref(prefName)) {
        mruList = prefSvc.prefs.getPref(prefName);
    } else {
        // Create a preference-set with this name.
        mruList = Components.classes["@activestate.com/koOrderedPreference;1"]
                  .createInstance();
        mruList.id = prefName;
        // Add the MRU list to the global preference set.
        prefSvc.prefs.setPref(prefName, mruList);
    }

    // If the mru list already contains this entry, first remove it so
    // that it will be reinserted at the top of the list again.
    for (var i = 0; i < mruList.length; i++) {
        var existingEntry = mruList.getStringPref(i);
        if (caseSensitive && entry == existingEntry) {
            mruList.deletePref(i);
            break;
        } else if (entry.toLowerCase() == existingEntry.toLowerCase()) {
            mruList.deletePref(i);
            break;
        }
    }

    // Also: keep the list constrained to the correct size.
    var maxEntries = MRU_maxEntries(prefName);
    while (mruList.length >= maxEntries && mruList.length > 0) {
        mruList.deletePref(mruList.length - 1);
    }

    // "Push" the entry onto the mru "stack".
    if (maxEntries > 0) {
        mruList.insertStringPref(0, entry);
    }

    _notifyOfMRUChange(prefName);
}


this.addURL = function MRU_addURL(prefName, url)
{
    _log.info("MRU_addURL(prefName="+prefName+", url="+url+")");

    // Never want to add kodebugger:// URLs to our MRUs.
    // XXX:HACK This should be a special case of a sub-classed implementation
    //          of koIMRU for the recent files and projects MRUs.
    if (url.indexOf("kodebugger://") == 0) {
        return;
    }

    //XXX Might want to cache or preprocess this.
    var infoService = Components.classes['@activestate.com/koInfoService;1'].
                      getService(Components.interfaces.koIInfoService);
    // On *nix filesystems, files with same name different case are recognised
    // as a separate files they are the same file on win32.
    var caseSensitive = infoService.platform.substring(0,3) != "win";

    // max entries should itself come from a preference!
    MRU_add(prefName, url, caseSensitive);
}


this.addFromACTextbox = function MRU_addFromACTextbox(widget)
{
    var searchType = widget.getAttribute("autocompletesearch");
    var searchParam = widget.searchParam;
    var prefName;
    var errmsg;
    switch (searchType) {
    case "mru":
        prefName = searchParam;
        break;
    default:
        prefName = ko.stringutils.getSubAttr(searchParam, "mru");
    }
    if (!prefName) {
        errmsg = "MRU_addFromACTextbox: warning: could not determine "+
                 "pref name from search param: '"+searchParam+"'";
        _log.warn(errmsg);
        return;
    }
    if (!widget.value) {
        return;
    }

    _log.info("MRU_addFromACTextbox(widget): widget.value='"+widget.value+
                "', prefName="+prefName);
    MRU_add(prefName, widget.value, true);
}


this.get = function MRU_get(prefName, index /* =0 */)
{
    if (typeof(index) == "undefined" || index == null) index = 0;
    _log.info("MRU_get(prefName="+prefName+", index="+index+")");

    var prefSvc = Components.classes["@activestate.com/koPrefService;1"].
                  getService(Components.interfaces.koIPrefService);
    var retval = null;
    if (!prefSvc.prefs.hasPref(prefName)) {
        retval = "";
    } else {
        var mruList = prefSvc.prefs.getPref(prefName);
        if (index >= mruList.length) {
            retval = "";
        } else {
            retval = mruList.getStringPref(index);
        }
    }
    _log.info("Returning: " + retval);
    return retval;
}

this.del = function MRU_del(prefName, index)
{
    _log.info("MRU_del(prefName="+prefName+", index="+index+")");
    // Remove the identified entry from the MRU list.
    var prefSvc = Components.classes["@activestate.com/koPrefService;1"].
                  getService(Components.interfaces.koIPrefService);
    if (prefSvc.prefs.hasPref(prefName)) {
        var mruList = prefSvc.prefs.getPref(prefName);
        mruList.deletePref(index);
        _notifyOfMRUChange(prefName);
    }
}

this.reset = function MRU_reset(prefName)
{
    _log.info("MRU_reset(prefName="+prefName+")");
    // Reset (a.k.a remove all elements) from the given MRU list.
    var prefSvc = Components.classes["@activestate.com/koPrefService;1"].
                  getService(Components.interfaces.koIPrefService);
    if (prefSvc.prefs.hasPref(prefName)) {
        var mruList = prefSvc.prefs.getPref(prefName);
        mruList.reset();
        _notifyOfMRUChange(prefName);
    }
}

/*
 * Returns an array of strings set in the given mru.
 * The order is from most recently used to least frequently used.
 * @public
 * @param prefName {string} name of the mru
 * @param maxLength {int} maximum number of entries to return, default is all.
 */
this.getAll = function MRU_getAll(prefName, maxLength /* all */)
{
    if (typeof(maxLength) == "undefined") maxLength = null;
    _log.info("MRU_get(prefName="+prefName+", maxLength="+maxLength+")");

    var prefSvc = Components.classes["@activestate.com/koPrefService;1"].
                  getService(Components.interfaces.koIPrefService);
    var retval = [];
    if (prefSvc.prefs.hasPref(prefName)) {
        var mruList = prefSvc.prefs.getPref(prefName);
        var startOffset = mruList.length - 1;
        var endOffset = -1;
        if (maxLength != null) {
            // Ensure the input is sane
            maxLength = Math.max(0, maxLength);
            // Set the endOffset so we only get a max of maxLength entries
            if (maxLength < mruList.length) {
                endOffset = (mruList.length - maxLength) - 1;
            }
        }
        for (var i=startOffset; i > endOffset; i--) {
            var entry = mruList.getStringPref(i);
            retval.push(entry);
        }
    }
    _log.info("Returning: " + retval);
    return retval;
}

}).apply(ko.mru);

// backwards compatibility api
var MRU_initialize = ko.mru.initialize;
var MRU_maxEntries = ko.mru.maxEntries;
var MRU_add = ko.mru.add;
var MRU_addURL = ko.mru.addURL;
var MRU_addFromACTextbox = ko.mru.addFromACTextbox;
var MRU_get = ko.mru.get;
var MRU_del = ko.mru.del;
var MRU_reset = ko.mru.reset;
var MRU_getAll = ko.mru.getAll;

