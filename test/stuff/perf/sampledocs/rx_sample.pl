# Copyright (c) 2000-2006 ActiveState Software Inc.
# See the file LICENSE.txt for licensing information.
#
# This sample Perl program shows you how Komodo's Rx Toolkit can help
# you debug regular expressions in your Perl code.

# This file illustrates matching mail headers.  The __DATA__ block, at
# the end of this file, contains a typical short email message. This
# Perl script will attempt to filter out the mail headers in this
# message using a regular expression.

use strict;
use Data::Dumper;

my %headers;
while (<DATA>) {
        if (/(.*):(.*)/) {
                $headers{$1} = $2;
        }
}
print Dumper \%headers;

# This code matches the "From" and "To" lines correctly; however, it
# is does not filter out all the mail headers.  To see for yourself:
#
#       1. Run this script: on the Debug toolbar, click "Go".
#
#       2. Open the Output pane and look at the output. This script
#          a) matched the initial "From sox@" line, which should have
#             been skipped; and
#          b) matched the "Re" from the "Subject" line too early.

# It's time to use Rx Toolkit!
#
#       1. Select the regular expression text inside the "/" delimiters:
#               (.*):(.*)
#
#       2. On the standard toolbar, click Rx.  Komodo loads the
#          regular expression into Rx Toolkit.
#
#       3. Enter a string to match your regular expression against.
#          Select the text after the __DATA__ token (at the end of
#          this file).
#
#       4. Check the "Global" modifier option to have Rx Toolkit match
#          each line of data.
#
#       5. Click "Advanced" button to see the group match variables.
#
# The green highlighting shows which parts of the data the regular
# expression matches. You can edit the regular expression in Rx
# Toolkit and see the match results immediately.
#
# a) The regular expression matched the first line even though it's
#    not a valid email header.  The match is too broad: (.*) matches
#    any character any number of times, then a colon, then (.*)
#    matches any character any number of times.  You can narrow the
#    matches by requiring some whitespace after the colon. Change the
#    regular expression to this:
#               (.*):\s+(.*)
#
# b) It picked up the "Re" from the "Subject" line as part of the
#    first group. That's because there is another ":" after the
#    "Re". We can specify that only alphanumeric characters are
#    allowed before the ":".  Change the loaded regular expression to
#    this:
#               (\w*):\s+(.*)
#
# Now paste your debugged regular expression back into your code.

# For more information on regular expressions, see "Introduction to
# Regular Expressions" in Komodo's online help.  For more information
# on Rx Toolkit see "Using Rx Toolkit" in Komodo's online help.  Just
# press <F1>, or select Help from the Help menu.  To see other
# features Komodo provides for your Perl coding, see "perl_sample.pl"
# in this sample project.


__DATA__
From sox@laundry.com Wed Mar 28 12:02:04 2001
Date: Tue, 27 Mar 2001 14:48:53 -0800
From: Steven Sox <sox@laundry.com>
To: Eric Herrington <red@herring.com>
Subject: Re: are you done yet?

> When are you going to be finished your book, man? I've still only
> read the second draft. That was two months ago!

I finally finished it last night. Thanks so much for your support over
the last couple of months. This should be a best seller, "My Head Is
Shaped Like An Egg," by Steven Sox. Sounds good, eh?

One can always hope, anyway.

Later,
Steve.
