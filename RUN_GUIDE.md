# Run Guide

Activate your Python env, then `cd` into the cloned project folder.

## Full run

```powershell
python main.py --playlist-url "https://youtube.com/playlist?list=YOUR_PLAYLIST_ID" --cookies-file cookies.txt
```

## First 2 videos

```powershell
python main.py --playlist-url "https://youtube.com/playlist?list=YOUR_PLAYLIST_ID" --cookies-file cookies.txt --max-videos 2
```

## URLs only

```powershell
python main.py --playlist-url "https://youtube.com/playlist?list=YOUR_PLAYLIST_ID" --cookies-file cookies.txt --enumerate-only
```

## Resume from video 51

```powershell
python main.py --playlist-url "https://youtube.com/playlist?list=YOUR_PLAYLIST_ID" --cookies-file cookies.txt --start-index 51
```

## Retry last failures

```powershell
python main.py --playlist-url "https://youtube.com/playlist?list=YOUR_PLAYLIST_ID" --cookies-file cookies.txt --retry-failed
```

## Output

- `output/transcripts.txt`
- `output/playlist_urls.txt`
