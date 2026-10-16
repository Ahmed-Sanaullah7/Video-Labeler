# Video Speaker Labeler Clipper

A small Windows desktop app for labeling **who speaks first** in each video: the **on-screen** actor or the **off-screen** speaker.
The label tells the model which diarized audio track belongs to the on-screen actor (diarization tags the first speaker as **A**):

| Who speaks first | On-screen actor's diarized track |
|---|---|
| On-screen  | **A** |
| Off-screen | **B** |
| Unclear    | (blank; leave it out of training) |

The app has three tools, picked on the start screen:

1. **Video Downloader**: download videos into one folder: the links in a spreadsheet, links you paste (YouTube, TikTok, Instagram, Facebook, X, Vimeo and many more sites), every video on a channel or profile, or the results of keyword searches. Save the videos, or only their audio. Optionally only videos whose license allows reuse. See [Download videos](#download-videos).
2. **Video Splitter**: cut long videos (30–50+ min, where the off-screen speaker keeps changing) into short clips, optionally cropped to part of the picture. See [Split long videos](#split-long-videos).
3. **Video Labeler**: label who speaks first in each clip. See [How to label](#how-to-label).

The **⌂** button at the top left goes back to the start screen. Switch between the tools with the **Download**, **Split** and **Label** tabs in the top bar. An open project stays open, so switching keeps your place.

## Install (Windows app)

1. Download `VideoLabeler-Setup-<version>.exe` from the [latest release](https://github.com/iqrarwaqas/Video-Labeler/releases/latest) and run it. No admin rights are needed.
2. Start **Video Speaker Labeler** from the Start menu. The app opens in its own window; close the window to quit.

The app uses the Microsoft Edge WebView2 runtime, which comes with Windows 11 and up-to-date Windows 10.

**Updates:** the app checks for a new version when it starts and shows a banner when one is available. You can also check at any time with **⚙ Settings → Check for updates** (or **Check for updates** on the start screen). Click **Update now**: the app closes, installs the new version and reopens. Your labels and settings are kept.

**Theme:** pick **System**, **Light** or **Dark** in **⚙ Settings**. **System** follows the Windows setting.

Windows may show "Windows protected your PC" because the installer isn't code-signed. Click **More info → Run anyway**.

## Setup from source (once)

```bash
pip install -r requirements.txt
```

## Run

```bash
python main.py --videos "D:\data\videos" --project "Batch_01"
```

The app opens in its own window. With `--videos`, the labeler opens straight away. You can also run `python main.py` with no arguments, pick a tool on the start screen and fill in the folders there (**Browse** opens a folder picker). The app remembers the last folders and settings you used in each tool.

Options:

| Flag | Meaning |
|---|---|
| `--videos DIR` | Folder with the videos (`.mp4 .mov .webm .mkv .avi .m4v`) |
| `--project NAME` | Project name, shown in the app and used for the output file name (default: the videos folder name) |
| `--output DIR` | Where `<project>_labels.xlsx` is written (default: an `output` folder next to the videos folder) |
| `--import FILE` | Seed labels from the old sheet (`Video_Name`, `Onscreen_Speaker` = a/b). Existing labels are never overwritten. |
| `--browser` | Open the app in the web browser instead of its own window. **Browse** buttons still work in a browser on the same PC (they open a Windows picker); you can always type or paste a path instead. |
| `--port N` | Port of the local server (default 5000) |
| `--host 0.0.0.0` | Let other PCs on the network open the app in their browser at `http://<this-pc>:<port>` |

## Download videos

The **Settings** panel on the left holds everything below, with **Start download** at its bottom. On the right are the **Overview** (counts and progress), the **Videos** list and the **Activity** log.

1. On the start screen, click **Video Downloader**. Under **Source**, pick where the videos come from:
   - **Sheet**: pick the spreadsheet (`.xlsx` or `.csv`) with the video links (**Browse** opens a file picker). The app reads the sheet and lists the links it found. It picks the column with the links, and a column for the file names (e.g. `Name` or `Title`), by itself. To change them, pick the **Sheet**, **Link column** and **Name column** yourself. **Name column → Video title** names each file after the video's own title instead.
   - **Links**: paste one or more video links, one per line. They're listed as you type. Any site [yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) supports works: YouTube, TikTok, Instagram, Facebook, X, Reddit, Vimeo, Dailymotion, Twitch, Bluesky, Pinterest, LinkedIn and many more, plus direct links to video files.
   - **Channel**: paste a channel or profile link (`https://www.youtube.com/@name`, or just `@name`) and, for YouTube, tick what to include: **Videos**, **Live** (past live streams) and **Shorts**. Click **Find videos** to list the whole channel, newest first. Profiles and channels on Vimeo, Twitch, Dailymotion, Rumble, VK and Bilibili work too, and so does a playlist link. **TikTok** doesn't let profile pages be read, so for a TikTok profile paste the link of **any one of its videos**: the app lists the whole profile from it. Instagram, Facebook and X profiles can't be listed: paste the links of their posts under **Links** instead.
   - **Search**: type what to search YouTube for, one search per line, and how many **Results per search** to take. Click **Find videos** (or press `Ctrl+Enter`). A video that turns up in several searches is listed once.
2. Optional: tick **License → Only reuse-allowed videos** to keep only videos whose license allows reuse: Creative Commons **CC BY**, **CC BY-SA** or **CC0** (on YouTube, the "Creative Commons Attribution (reuse allowed)" license). Each video's license is checked just before it downloads, and the others are marked **Not allowed** and skipped without downloading. Non-commercial (NC) and no-derivatives (ND) licenses, such as most TEDx talks, don't count, and neither do links with no license to check (e.g. a direct `.mp4` link). With **Search**, YouTube's own Creative Commons filter is used too, so the list is mostly reusable videos to begin with.
3. Under **Download → Save as**, pick **Video**, or **Audio only** to keep just the sound (from any site: YouTube, TikTok, Facebook, Instagram and the rest, or a direct link to a video file).
   - For a video, pick the **Quality** (default 1080p). It counts the short side of the picture, so a vertical 1080×1920 video (TikTok, Shorts) is 1080p. Where a video comes in several codecs, the app avoids HEVC (TikTok's usual codec), which many PCs can't play, even if that means a slightly smaller picture.
   - For audio, pick the **Audio format**: **M4A** (kept as the site serves it where possible, otherwise AAC at 192 kbps), **MP3** (192 kbps) or **WAV** (uncompressed). TikTok's separate audio track is the post's music (often a whole song the video borrowed), so the app takes the sound from the video itself.
   - **Parallel downloads**: how many download at once (default 3; YouTube starts throttling above 4).
   - **H.264 video** (optional, for video): the most compatible MP4. YouTube otherwise often sends AV1 video, which some players and editors can't open.
   - **Follow playlists** (optional): for a link to a video in a playlist, download the whole playlist instead of only that video.
   - **Advanced settings**: attempts and timeout per video, download only the first N videos (for a channel, the newest N), and **Cookies from** a browser you're signed in with, for age-restricted or members-only videos, or when YouTube asks to confirm you're not a bot.
4. Pick the **Output folder**. For a sheet you can leave it empty for a `downloads` folder next to the spreadsheet. Click **Start download**. **Stop** ends the run after the chunks that are in flight.
   - **Resume** (after a stop) goes on with the videos that aren't done yet; **Retry failed** tries the failed ones again, and **↻** on a failed row retries just that video. Both use the list on screen, so finished videos keep their status and files are named as before. Half-downloaded YouTube videos continue where they stopped.
   - **Start download** again runs the whole list; videos already in the folder are skipped, so it also picks up where an earlier session left off (for example after closing the app).
5. When it's done, **Open in Splitter** opens the Splitter setup with the download folder filled in, to cut the long videos into clips (not shown for audio).

**Dataset** (the tab next to **Videos**) sums up everything in the output folder, across every run: how many videos and audio files, their total length and size, the average length, how many (and how many hours) are reuse-allowed, the licenses, a length breakdown and the top channels. The app keeps what it learns about each video in `_dataset.json` in the folder; videos it knows nothing about are measured once with ffmpeg.

Notes:

- **Videos already in the output folder are skipped**, so you can run the same sheet again after adding rows, or to retry the ones that failed. With **Audio only**, it's audio files in the chosen format that are skipped, so audio and video of the same videos can share a folder.
- Every run writes `_download_report.csv` (one row per video: status, file, title, channel, license, runtime, size, error) and adds a summary to `report.txt` in the output folder. **Open report** opens `report.txt`.
- **Social media:** Instagram and Facebook often only show videos to a signed-in browser. If a download fails asking you to log in, set **Advanced settings → Cookies from** to a browser where you're signed in. Social media sites don't publish a license, so with **Only reuse-allowed videos** on their videos are skipped as **Not allowed**. Long captions (TikTok, Instagram) are cut to 100 characters in the file name.
- **With Only reuse-allowed videos, every saved video gets a `<video>.attribution.txt`** next to it: title, channel, link, license and a suggested credit line. CC BY and CC BY-SA require crediting the creator wherever the clips are used.
- The license check uses the license the uploader chose. Mislabeled uploads do exist, so spot-check a few before relying on a large batch. Videos already in the output folder from an earlier run are skipped, not checked: use a fresh folder for a reuse-allowed-only dataset.
- Links in cells with a label such as "Watch" work too: the app reads the cell's hyperlink. A bare YouTube video ID (11 characters) also works.
- Videos are saved as MP4. Double-click a downloaded video in the list to play it.
- Audio is fetched and converted in a `_audio_parts` folder inside the output folder, then moved up when it's finished. The folder is removed at the end of the run, unless a stopped run left a part to continue.
- Downloads use [yt-dlp](https://github.com/yt-dlp/yt-dlp). If YouTube changes something and downloads start failing, a newer version of the app usually fixes it.

## Split long videos

The splitter is laid out like a video editor: the **Media** panel on the left lists the long videos, the **Program** monitor in the middle plays the open video (with play, frame-step and mark buttons and a timecode underneath), the **Properties** panel on the right has the crop, the segment list and **Export clips**, and the **Timeline** runs along the bottom. The **▯** button in the top bar hides or shows the Media panel.

1. On the start screen, click **Video Splitter**. Pick the folder with the long videos and an **output (dataset) folder** for the clips. The output folder can't be the same as the long-videos folder.
2. Pick a video in the Media panel and play it (`Space`, the ▶ button or a click on the picture). Mark the parts you want to keep:
   - **Ranges:** press `I` (**Mark In**) where a part begins and `O` (**Mark Out**) where it ends, or use the buttons under the video. Everything between segments is left out.
   - **Markers:** press `M` to drop a split marker, then click **Segments from markers**. This cuts the whole video into back-to-back pieces at the markers (parts that overlap an existing segment are skipped). Delete the pieces you don't want.
3. Fine-tune: drag a segment to move it, drag its edges to resize it (the video shows the frame at the edge), or type exact times in the segment list (`1:02:03.500`, `2:03.5` or `123.5`). Segments can't overlap. Double-click a segment, or click ▶ in the list, to play just that segment.
4. Optional: crop the picture. In **Properties → Crop**, click **Edit crop** (or press `C`), then drag on the video to draw a box. Drag the box to move it and its corners or edges to resize it, or type exact pixel values in **X**, **Y**, **W** and **H**. Pick a shape (**Free**, **1:1**, **9:16** or **16:9**) to lock the box to it. **Reset** goes back to the full frame. Click **Done** (or press `C` or `Esc`) when you're finished; the box stays outlined on the video. The crop applies to every clip of this video.
5. Click **Export clips** (bottom of the Properties panel). Each segment is saved as its own MP4: `interview.mp4` → `interview_1.mp4`, `interview_2.mp4`, … Only segments that aren't exported yet are cut: add a segment later and **Export 1 clip** cuts just that one, while clips already exported are kept as they are. You can cancel at any time. Clips that are already finished are kept. Exported clips get a ✓ on the timeline and turn green in the segment list.
6. Click **Open in Labeler** to label the new clips. The labeler setup opens with the output folder filled in.

**Dataset** (the tab next to **Properties**) sums up the whole output folder: how many clips, their total time (all exported clips together, in hours once it passes an hour) and average length, how much of the long videos they use, how many long videos are split, in progress or not started, a clip-length breakdown, the clips per long video, segments not exported yet, and how many clips are cropped. Each long video's length is measured once with ffmpeg and kept in `_splits.json`.

Shortcuts: `Space` play/pause · `←` / `→` 5 s back/forward (`Shift`: 1 s) · `,` / `.` one frame · `↑` / `↓` previous/next segment edge or marker · `I` / `O` mark in/out · `M` marker · `C` crop · `Delete` delete the selected segment or marker · `Ctrl+Z` undo · `Ctrl`+mouse wheel or `+` / `-` zoom · `0` show the whole video · `Esc` cancel a started segment or finish cropping.

Notes:

- **Cuts are frame-accurate.** Clips are re-encoded to H.264/AAC MP4, so they always play in the labeler. This takes time: about 1–3 minutes of work for every 10 minutes of clips, depending on the PC.
- **Your segments and crop are saved as you work**, in `_splits.json` in the output folder. Open the same folders again to continue. The file also records which part of which source video every exported clip came from, including the crop box (in pixels of the source video).
- **Changing the crop after exporting** doesn't change clips that are already saved. Their names turn grey in the segment list again and lose their ✓ on the timeline; export again to update them.
- The crop's width and height are rounded to even pixels, because H.264 video needs an even size. With **9:16** or **16:9**, the clip's size can therefore be a pixel or two off the exact ratio (**1:1** is always exactly square).
- **A clip's name never changes and is never reused**, because the Labeler keeps labels by file name. New clips take the next free number, so working from the start of a video to the end they're in time order; a segment added earlier in the video later gets a higher number. The segment list shows which clip each segment is (or will be).
- **Changing or deleting an exported segment** (its times, or the video's crop) makes its old clip out of date. On the next export the app asks before deleting those old clips, then cuts the changed segments as new clips.
- In the Media panel, a video's dot is blue when it has exported clips and hollow when it has segments that aren't exported yet.
- ffmpeg comes with the Windows app. When you run from source, `pip install -r requirements.txt` installs it (`imageio-ffmpeg`). If that isn't possible, an `ffmpeg` on PATH is used instead.

## How to label

1. Watch the video.
2. Click **On-screen**, **Off-screen** or **Unclear / Skip** (or press `1` / `2` / `3`).
3. The label is saved straight away and the next video opens.

Shortcuts: `←` / `→` previous/next · `Space` play/pause · `R` replay (or the ↻ button under the video) · `N` next unlabeled video.
The Media panel lists every video with a colored dot (green = on-screen, orange = off-screen, grey = unclear), with a count per label. Use **All / To do / Done** to filter the list. **⚙ Settings → Show labels file** opens the output folder with the Excel file selected.

**Resume:** open the same project again and the app continues at the first unlabeled video. If you go back to a video you already labeled, its label is highlighted. Click another option to change it, or click **Clear label**.

## Output: `output/<project>_labels.xlsx`

| Project | Video_Name | Video_File | First_Speaker | Onscreen_Diarized_Label | Labeled_At |
|---|---|---|---|---|---|
| Batch_01 | video1 | video1.mp4 | onscreen | A | 2026-09-24 13:20:00 |
| Batch_01 | video4 | video4.mp4 | offscreen | B | 2026-09-24 13:21:10 |

- The file is updated after every click, and each video has only one row (changing a label updates that row).
- Each project has its own file, so several projects can share one output folder.
- If the output folder has an old `labels.xlsx` from before project names existed, its labels are copied into the first project you open there. The old file is left untouched.
- **Close the labels file in Excel while labeling.** Excel locks the file. If it's open, the app shows a warning and nothing is lost: close Excel and click the label again.
- Use one project per person. Two people must not label into the same project file at the same time.

## Troubleshooting

- **The app opens in the browser instead of its own window**: the WebView2 runtime is missing. Install it from [Microsoft](https://developer.microsoft.com/microsoft-edge/webview2/) and start the app again.
- **"This video can't be played here"**: `.avi` and some `.mkv`/`.mov` codecs can't be played. Convert them to MP4:
  `ffmpeg -i in.avi -c:v libx264 -c:a aac out.mp4`

## Releasing a new version (maintainers)

1. Update `__version__` in `main.py`, e.g. `"1.1.0"`, and commit.
2. Tag and push the tag:
   ```bash
   git tag v1.1.0
   git push origin v1.1.0
   ```
3. The **Release** GitHub Action builds `VideoLabeler-Setup-1.1.0.exe` and publishes it as a GitHub Release. Installed apps see the update the next time they start.

The tag must match `__version__`, or the build fails. The repo must be public so that installed apps can check for updates and download them.

To build the installer locally, install [Inno Setup 6](https://jrsoftware.org/isdl.php) and run:

```bash
pip install pyinstaller
pyinstaller packaging/VideoLabeler.spec --noconfirm
iscc /DAppVersion=1.1.0 installer\VideoLabeler.iss
```

The installer is written to `dist/`.

The app icon is `packaging/icon.ico` (installer and `.exe`) and `static/icon.svg` (inside the app). If you change the design, update `static/icon.svg` and redraw the `.ico` with `python packaging/make_icon.py` (needs Pillow).
