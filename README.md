# H2Sync

Keeps a Rockbox player (built for the HIFI WALKER H2, works with any music
folder) in sync with a [Navidrome](https://www.navidrome.org/) server.

- **Playlists to the device.** Choose Navidrome playlists, by name or name
  pattern. Their tracks are copied as the original files into
  `/Music/<Album Artist>/<Album> (<Year>)/<NN> - <Title>.<ext>`, with a
  `cover.jpg` per album and `/Playlists/<name>.m3u8` for Rockbox.
- **Plays back to the server.** Plays from Rockbox's `playback.log` are sent to
  Navidrome as scrobbles with their original timestamps. Navidrome forwards them
  to Last.fm or ListenBrainz if you've linked one.
- **Your own files are left alone.** Music already on the device that matches a
  server song is organized into the same layout and is never deleted. Files the
  server doesn't have are left where they are.
- **Nothing changes until you confirm.** Each run shows a plan first: what will
  be copied, moved and removed, how long it will take, and whether it fits.

## Portable

H2Sync runs from a folder on the card or player itself, on macOS, Windows and
Linux, with nothing installed on the computer. The folder includes a trimmed
[python-build-standalone](https://github.com/astral-sh/python-build-standalone)
runtime, with Tk, for each platform. The launcher unpacks it to the computer's
temp folder, so the card can be ejected while H2Sync is open.

Everything H2Sync keeps lives on the card:

| Where | What |
|---|---|
| `H2Sync/settings/` | servers and their logins: a Subsonic login token, not the password |
| `.h2sync/` (card root) | this device's choices, the files H2Sync manages, plays already sent |

## Using it

Download `H2Sync-<version>.zip` from
[Releases](https://github.com/fityp/h2sync/releases) and unzip it onto the card
or player, so that you have `H2Sync/` at the card root. Then start it from that
folder:

| Platform | How to start |
|---|---|
| macOS | double-click `H2Sync.app` |
| Windows (10 1803+ or 11) | double-click `Start H2Sync (Windows).cmd` |
| Linux (x86-64 or ARM64, desktop session) | `sh "Start H2Sync (Linux).sh"` |

The macOS app isn't signed. If macOS refuses to open it, right-click it and
choose Open.

From source, with Python 3.10+ and Tk:

```
python3 h2sync.py            # window
python3 h2sync.py plan       # print the plan
python3 h2sync.py sync       # sync in the terminal (--yes to skip the prompt)
python3 h2sync.py list       # server playlists and which ones sync
```

Options: `--home DIR` (folder holding `settings/`), `--device PATH`,
`--server NAME`.

## Building the portable folder

```
python3 build_portable.py                  # builds dist/H2Sync
python3 build_portable.py --to /Volumes/H2 # also copies it onto a card
```

Copying onto a card keeps the card's `H2Sync/settings` folder.
