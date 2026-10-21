H2Sync - keep this player in sync with Navidrome
=================================================

Nothing needs to be installed. Start it from this folder:

  macOS    double-click H2Sync.app
           (first time only: if macOS asks to let H2Sync find devices on
           your local network, choose Allow - that is how it reaches Navidrome)

  Windows  double-click "Start H2Sync (Windows).cmd"
           (Windows 10 version 1803 or later, or Windows 11)

  Linux    in a terminal:  sh "/path/to/H2Sync/Start H2Sync (Linux).sh"
           (needs a desktop session; x86-64 or ARM64)

In the window pick the device (this card is chosen automatically), the
server, and the playlists to keep on the device, then press Start sync.
Nothing changes on the device until you do.

What lives where
  H2Sync/settings/        servers and their logins. The password itself is not
                          saved, only a login token made from it; anyone with
                          this card can use it to sign in to your Navidrome
                          account. Changing the Navidrome password revokes it.
  .h2sync/  (card root)   this device's playlist choices, the list of files
                          H2Sync manages, and which plays were already sent.

The computer only gets a disposable copy of Python in its temp folder, so the
card can be ejected while H2Sync is open.
