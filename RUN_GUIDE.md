# Run Guide

## Every session

```powershell
conda activate yt_playlist_text
cd "C:\Users\ha997\Desktop\YT_playlist_to_text"
```

---

## Standard full run

```powershell
python main.py --playlist-url "https://youtube.com/playlist?list=YOUR_PLAYLIST_ID" --cookies-file "C:\Users\ha997\Desktop\YT_playlist_to_text\cookies.txt"
```

---

## Common run modes

### Test on first 2 videos
```powershell
python main.py --playlist-url "https://youtube.com/playlist?list=YOUR_PLAYLIST_ID" --cookies-file "C:\Users\ha997\Desktop\YT_playlist_to_text\cookies.txt" --max-videos 2
```

### Preflight check (verify all URLs found before transcribing)
```powershell
python main.py --playlist-url "https://youtube.com/playlist?list=YOUR_PLAYLIST_ID" --cookies-file "C:\Users\ha997\Desktop\YT_playlist_to_text\cookies.txt" --expected-videos 164 --strict-enumeration --enumerate-only
```

### Resume from a specific position
```powershell
python main.py --playlist-url "https://youtube.com/playlist?list=YOUR_PLAYLIST_ID" --cookies-file "C:\Users\ha997\Desktop\YT_playlist_to_text\cookies.txt" --start-index 51
```

### Re-run only failed videos
```powershell
python main.py --playlist-url "https://youtube.com/playlist?list=YOUR_PLAYLIST_ID" --cookies-file "C:\Users\ha997\Desktop\YT_playlist_to_text\cookies.txt" --retry-failed
```

---

## Output locations

- Transcripts: `output/transcripts.txt`
- URL manifest: `output/playlist_urls.txt`
