# Third-party notices

BA Player is Copyright (C) 2026 Brian Scott and is licensed under the GNU General Public
License, version 3 or (at your option) any later version. See `LICENSE.txt`.

The built `.exe` files and the web page use the following third-party software. Their
licenses allow this use; their notices are reproduced or linked here as they require.

## Included in the .exe files

**Python** (the runtime PyInstaller packs into the .exe)
Copyright (c) 2001 Python Software Foundation. All rights reserved.
Licensed under the Python Software Foundation License Version 2.
https://docs.python.org/3/license.html

**Tcl/Tk** (used by tkinter for the BA Player window)
Copyright (c) the Regents of the University of California, Sun Microsystems, Inc.,
Scriptics Corporation, ActiveState Corporation and other parties.
Licensed under the Tcl/Tk license (BSD-style). https://www.tcl.tk/software/tcltk/license.html

**PyInstaller bootloader** (the small program that starts the packed .exe)
Licensed under the GNU GPL v2 or later with the PyInstaller bootloader exception, which
allows programs built with it to be distributed under any license.
https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt

## Loaded by the web page at run time (not included)

**dash.js** 4.7.4, the player on the Watch pages, loaded from the jsDelivr CDN.
Copyright (c) 2023, Dash Industry Forum. All rights reserved.
Licensed under the BSD 3-Clause License. https://github.com/Dash-Industry-Forum/dash.js

## Not included: supplied by the user

**ffmpeg-ac4** (https://github.com/ferrellsl/ffmpeg-ac4), used for sound on internet
channels, is not distributed with BA Player; each user builds or obtains their own.
FFmpeg is licensed under the LGPL v2.1 or later (GPL if built with GPL parts). Its AC-4
decoder is an unofficial implementation of a Dolby format; whether using it needs a
patent license depends on where you are.

## Other people's work BA Player works with

- **RENDER reports** come from RabbitEars (https://www.rabbitears.info), made with RENDER,
  the RabbitEars NextGen Data Evaluator (GPL v3). BA Player reads the reports; it contains
  no RENDER code. The sample `reports\sample.html` is a RENDER report of Brian Scott's
  own El Paso (RF 15) capture.
- **Broadcaster apps and streams** belong to their stations and vendors. BA Player only
  opens them in your browser, the way a TV would; it doesn't copy or change them.
- Station names and logos are trademarks of their owners.
