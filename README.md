# Seas Audio Tools

Two audio-housekeeping tools behind one drag-and-drop window:

| Tool | What it does |
| --- | --- |
| **Sanitize** | Rewrites embedded cover art to baseline JPEG and forces ID3v2.3, so car stereos and TVs stop choking on the tags. |
| **Duplicates** | Fingerprints a library with Chromaprint and reports near-identical tracks, including re-encodes and offset copies. |

Drop a file or a folder onto the window, pick the tab, press Run.

## Layout

```
bin/fpcalc.exe          Chromaprint binary (v1.6.1)
src/seas_audio/core     scanner, fpcalc wrapper, cache, settings
src/seas_audio/tools    sanitizer + duplicates (no cross-imports)
src/seas_audio/ui       Tkinter shell with tkinterdnd2 drop targets
tests                   unittest suites, runnable with pytest
build                   PyInstaller spec
```

## Install (development)

```powershell
cd D:\Projects\seas-audio-tools
python -m pip install -e ".[build]"
```

## Run

```powershell
seas-audio-gui                       # the drop-zone window
seas-audio-gui "E:\Music\Some Album" # pre-loaded with a path

seas-audio sanitize "E:\Music\Random MP3s" --dry-run
seas-audio sanitize "E:\Music\Random MP3s" --backup
seas-audio dupes "E:\Music\Random MP3s" --report duplicates.csv
seas-audio dupes "E:\Music\Telegram Desktop" --reference "E:\Music\Random MP3s"
seas-audio cache clear
```

## Performance

The original pair scan walked a NumPy array in a Python loop. On 2,211 tracks
that was 2,443,155 pairs at ~25 ms each - about 17 hours worst case.

Two changes, both measured on the real library cache:

* `np.bitwise_count` replaces `sum(int(x).bit_count() for x in ...)` - 13.5x.
* Pairs are only generated inside a +/-5 s duration window - 93.8% fewer pairs
  (2,443,155 -> 151,124).

Net result: worst case drops to roughly five minutes.

## Cache

Fingerprints live in `%LOCALAPPDATA%\seas-audio-tools\cache\fingerprints.pkl`,
keyed by absolute path, with a schema version. Only successful fingerprints are
stored, so unreadable files are retried on the next run instead of being
cached as permanently broken.

Set `SEAS_AUDIO_HOME` to relocate all app data.

## Build a standalone exe

```powershell
cd D:\Projects\seas-audio-tools
python -m PyInstaller build\seas-audio.spec --noconfirm
```

Output lands in `build\dist\SeasAudioTools\`. The `tkinterdnd2` hook ships with
PyInstaller's contrib hooks, so the drop targets survive packaging.