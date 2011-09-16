/**
 * Notifications widget
 */

var NotificationsWidgetController = {};

(function() {

const Ci = Components.interfaces;
const Cc = Components.classes;
const Cu = Components.utils;

Cu.import("resource://gre/modules/XPCOMUtils.jsm");

/**
 * Initialize the notification widget
 */
this.onload = (function NotificationsWidgetController_onload() {
  this._map = {}; // identifier -> array of elem
  ko.notifications.addListener(this);
  // load the already existing notifications
  for each (var notification in ko.notifications.getNotifications([parent, null], 2)) {
    this.addNotification(notification);
  }
  this.container.controllers.insertControllerAt(0, notificationsController);
  this.container.addEventListener("keypress", notificationsController, false);

  // exported for SCC
  window.focusNotification = this.focusNotification;
}).bind(this);

/**
 * Destroy the notification widget
 */
this.onunload = (function NotificationsWidgetController_onunload() {
  ko.notifications.removeListener(this);
  this.container.controllers.removeController(notificationsController);
  this.container.removeEventListener("keypress", notificationsController, false);
}).bind(this);

/**
 * Add a notification to the list of displayed notifications
 * (Also, remove old ones if too many are around)
 */
this.addNotification = (function NWC_addNotification(notification) {
  var elem = document.createElement("notification");
  this.container.appendChild(elem);
  elem.text = [notification.summary, notification.description]
              .filter(function(n) n).join(": ");
  elem.tags = notification.getTags();
  elem.identifier = notification.identifier;
  elem.notification = notification;
  elem.iconURL = notification.iconURL;
  elem.time = new Date(notification.time / 1000); // usec_per_msec
  elem.type = ["info", "warning", "error"][notification.severity];
  if (notification instanceof Ci.koINotificationText) {
    elem.details = notification.details;
  }
  elem.collapsed = !this.shouldShowItem(elem);

  // build a map of notifications to make removes faster
  if (notification.identifier in this._map) {
    this._map[notification.identifier].push(elem);
  } else {
    this._map[notification.identifier] = [elem];
  }

  // Limit the number of notifications
  var maxItems = 50;
  if (this.prefs.hasLongPref("notifications.ui.maxItems")) {
    maxItems = this.prefs.getLongPref("notifications.ui.maxItems");
  }
  while (this.container.children.length > maxItems) {
    this.removeNotification(this.container.children[0].notification);
  }
}).bind(this);

/**
 * Remove a notification from the list of displayed notifications
 */
this.removeNotification = (function NWC_removeNotification(notification) {
  var candidates = this._map[notification.identifier] || [];
  for (var [index, candidate] in Iterator(candidates)) {
    if (candidate.notification.contxt == notification.contxt) {
      // found it
      this.container.removeChild(candidate);
      candidates.splice(index, 1);
      if (candidates.length < 1) {
        delete this._map[notification.identifier];
      }
      break;
    }
  }
}).bind(this);

/**
 * Handler for a notification change from the notification manager
 */
this.onNotification = (function NWC_onNotification(notification, oldIndex, newIndex, reason) {
  if (reason == Ci.koINotificationListener.REASON_UPDATED) {
    if (notification instanceof Ci.koIStatusMessage && !notification.summary) {
      // a status message with no text and no description - this is it being removed
      // ignore this change, we'd rather keep the old text
      return;
    }
  }
  // unconditionally kill the old one if it exists
  this.removeNotification(notification);
  if (reason != Ci.koINotificationListener.REASON_REMOVED) {
    // updated or added - add it (back) in
    if (!notification.contxt || notification.contxt == parent) {
      this.addNotification(notification);
    }
  }
}).bind(this);

/**
 * Filter the displayed notifications in response to the search filters changing
 */
this.updateFilters = (function NWC_updateFilters() {
  for each (var elem in this.container.children) {
    elem.collapsed = !this.shouldShowItem(elem);
  }
}).bind(this);

/**
 * Determine whether a given item should be shown
 * @param aElem {richlistitem} The item to check
 * @returns true if the item should be displayed
 */
this.shouldShowItem = (function NWC_shouldShowItem(aElem) {
  var types = {};
  var any = false;
  for each (let type in ["info", "warning", "error"]) {
    let checked = document.getElementById("filter-" + type).checked;
    types[Ci.koINotification["SEVERITY_" + type.toUpperCase()]] = checked;
    any |= checked;
  }
  types[Ci.koINotification.SEVERITY_ERROR] = document.getElementById("filter-error").checked;
  var progress = document.getElementById("filter-progress").checked;
  var textSearch = document.getElementById("filter-search").value.toLowerCase();
  var show = types[aElem.notification.severity] || !any;
  if (progress) {
    show &= (aElem.notification instanceof Ci.koINotificationProgress);
  }
  if (textSearch.replace(/\W/g, '').length > 0) {
    // check for summary matching complete search text
    var matchSearch = (aElem.searchText.indexOf(textSearch.replace(/\W/g, '')) != -1);
    if (!matchSearch) {
      // check for tags matching any word
      for each (var word in textSearch.split(/\W+/)) {
        if (matchSearch) break; // already found
        if (word.length < 1) continue; // empty word
        for each (var tag in aElem.notification.getTags()) {
          if (tag.replace(/\W/g, '').substr(0, word.length).toLowerCase() == word) {
            // word is a prefix of the tag
            matchSearch = true;
            break;
          }
        }
      }
    }
    show &= matchSearch;
  }
  return show;
}).bind(this);

/**
 * Try to focus (and expand) a given notification, collapsing others
 * @param notification {koINotification} The notification to focus
 */
this.focusNotification = (function NWC_focusNotification(notification) {
  var elem = null;
  for each (var child in this.container.children) {
    if (notification == child.notification) {
      elem = child;
      break;
    }
  }
  if (elem) {
    for each (child in this.container.children) {
      // close everythin else, and open only the element to focus
      child.open = (child === elem);
    }
    // select it and make sure it's visible
    this.container.selectedItem = elem;
    // need to wait a bit to make sure the element is properly opened
    setTimeout(this.container.ensureElementIsVisible.bind(this.container, elem),
               10);
    }
}).bind(this);

/**
 * The list box containing the displayed notifications
 */
XPCOMUtils.defineLazyGetter(this, "container",
                            function() document.getElementById("message-container"));

/**
 * The global prefs service
 * (not a lazy getter because it depends on the active project)
 */
Object.defineProperty(this, "prefs", {
  get: function() Cc["@activestate.com/koPrefService;1"]
                    .getService(Ci.koIPrefService).effectivePrefs,
  enumerable: true,
});

/**
 * Command controller
 */
var notificationsController = new xtk.Controller();

notificationsController.is_cmd_copy_supported =
notificationsController.is_cmd_selectAll_supports =
  function notificationsController_supports_copy()
    document.commandDispatcher.focusedElement == NotificationsWidgetController.container;

notificationsController.is_cmd_copy_enabled = function notificationsController_is_copy_enabled() {
  return NotificationsWidgetController.container.selectedItems.length > 0;
}

notificationsController.do_cmd_copy = function notificationsController_do_copy() {
  var buffers = [], buffer = "";
  // try to get selected text in _open_ notifications
  // (ignore closed ones because the selection isn't visible)
  var ranges = [];
  var selection = window.getSelection();
  var container = NotificationsWidgetController.container;
  for (var i = 0; i < selection.rangeCount; ++i) {
    var range = selection.getRangeAt(i);
    if (range.collapsed) {
      // collapsed range, not interesting
      continue;
    }
    var notification = range.commonAncestorContainer;
    while (notification && notification.localName != "notification") {
      notification = document.getBindingParent(notification);
    }
    if (!notification || !notification.open) {
      // isn't in a notification, or is closed
      continue;
    }
    // this range is in an open notification, take it
    buffers.push(range.toString());
  }

  if (buffers.length > 0) {
    // got usefully selected text; put that in the clipboard
    buffer = buffers.join("");
  } else {
    // no useful selected text; just copy the selected notifications
    for each (var child in container.selectedItems) {
      var time = child.time.toString();
      // right align time to 80 columns
      time = Array(Math.max(0, 81 - time.length)).join(" ") + time;
      var entry = [child.text, time];
      if (child.details)
        entry = entry.concat(["  " + details for each (details in child.details.split("\n"))]);
      buffers.push(entry.join("\n"));
    }
    buffer = buffers.join("\n" + Array(81).join("-") + "\n");
  }
  // dump the text into the clipboard
  Cc["@mozilla.org/widget/clipboardhelper;1"]
    .getService(Ci.nsIClipboardHelper)
    .copyString(buffer);
};

notificationsController.do_cmd_selectAll = function notificationsController_do_selectAll()
  NotificationsWidgetController.container.selectAll();

/** nsIDOMEventListener */
notificationsController.handleEvent = function(event) {
  switch (event.keyCode) {
    case KeyEvent.DOM_VK_LEFT:
      NotificationsWidgetController.container.selectedItems.forEach(function(n) n.open = false);
      break;
    case KeyEvent.DOM_VK_RIGHT:
      NotificationsWidgetController.container.selectedItems.forEach(function(n) n.open = true);
      break;
    case KeyEvent.DOM_VK_ENTER:
    case KeyEvent.DOM_VK_RETURN:
      NotificationsWidgetController.container.selectedItems.forEach(function(n) n.open = !n.open);
      break;
  }
};

addEventListener("load", this.onload, false);
addEventListener("unload", this.onunload, false);

}).apply(NotificationsWidgetController);