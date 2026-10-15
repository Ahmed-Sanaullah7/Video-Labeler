# Video Speaker Labeler Clipper

A small Windows desktop app for labeling **who speaks first** in each video: the **on-screen** actor or the **off-screen** speaker.
The label tells the model which diarized audio track belongs to the on-screen actor (diarization tags the first speaker as **A**):

| Who speaks first | On-screen actor's diarized track |
|---|---|
| On-screen  | **A** |
| Off-screen | **B** |
| Unclear    | (blank; leave it out of training) |

The app has three tools, picked on the start screen:

1. **Video Downloader**: download every video linked in a spreadsheet (YouTube, other video sites, or direct file links) into one folder. See [Download videos](#download-videos).
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
| `--browser` | Open the app in the web browser instead of its own window |
| `--port N` | Port of the local server (default 5000) |
| `--host 0.0.0.0` | Let other PCs on the network open the app in their browser at `http://<this-pc>:<port>` |

## Download videos

The **Settings** panel on the left holds everything below, with **Start download** at its bottom. On the right are the **Overview** (counts and progress), the **Videos** list and the **Activity** log.

1. On the start screen, click **Video Downloader**. Pick the spreadsheet (`.xlsx` or `.csv`) with the video links (**Browse** opens a file picker).
2. The app reads the sheet and lists the links it found. It picks the column with the links, and a column for the file names (e.g. `Name` or `Title`), by itself. To change them, pick the **Sheet**, **Link column** and **Name column** yourself. **Name column → Video title** names each file after the video's own title instead.
3. Pick the **Quality** (default 1080p) and how many videos download at once (**Parallel downloads**, default 3; YouTube starts throttling above 4). Optional:
   - **H.264 video**: the most compatible MP4. YouTube otherwise often sends AV1 video, which some players and editors can't open.
   - **Follow playlists**: for a link to a video in a playlist, download the whole playlist instead of only that video.
   - **Advanced settings**: attempts and timeout per video, download only the first N rows, and **Cookies from** a browser you're signed in with, for age-restricted or members-only videos.
4. Pick the **Output folder**, or leave it empty for a `downloads` folder next to the spreadsheet. Click **Start download**. **Stop** ends the run after the chunks that are in flight.
5. When it's done, **Open in Splitter** opens the Splitter setup with the download folder filled in, to cut the long videos into clips.

Notes:

- **Videos already in the output folder are skipped**, so you can run the same sheet again after adding rows, or to retry the ones that failed.
- Every run writes `_download_report.csv` (one row per link: status, file, title, runtime, size, error) and adds a summary to `report.txt` in the output folder. **Open report** opens `report.txt`.
- Links in cells with a label such as "Watch" work too: the app reads the cell's hyperlink. A bare YouTube video ID (11 characters) also works.
- Videos are saved as MP4. Double-click a downloaded video in the list to play it.
- Downloads use [yt-dlp](https://github.com/yt-dlp/yt-dlp). If YouTube changes something and downloads start failing, a newer version of the app usually fixes it.

## Split long videos

The splitter is laid out like a video editor: the **Media** panel on the left lists the long videos, the **Program** monitor in the middle plays the open video (with play, frame-step and mark buttons and a timecode underneath), the **Properties** panel on the right has the crop, the segment list and **Export clips**, and the **Timeline** runs along the bottom. The **▯** button in the top bar hides or shows the Media panel.

1. On the start screen, click **Video Splitter**. Pick the folder with the long videos and an **output (dataset) folder** for the clips. The output folder can't be the same as the long-videos folder.
2. Pick a video in the Media panel and play it (`Space`, the ▶ button or a click on the picture). Mark the parts you want to keep:
   - **Ranges:** press `I` (**Mark In**) where a part begins and `O` (**Mark Out**) where it ends, or use the buttons under the video. Everything between segments is left out.
   - **Markers:** press `M` to drop a split marker, then click **Segments from markers**. This cuts the whole video into back-to-back pieces at the markers (parts that overlap an existing segment are skipped). Delete the pieces you don't want.
3. Fine-tune: drag a segment to move it, drag its edges to resize it (the video shows the frame at the edge), or type exact times in the segment list (`1:02:03.500`, `2:03.5` or `123.5`). Segments can't overlap. Double-click a segment, or click ▶ in the list, to play just that segment.
4. Optional: crop the picture. In **Properties → Crop**, click **Edit crop** (or press `C`), then drag on the video to draw a box. Drag the box to move it and its corners or edges to resize it, or type exact pixel values in **X**, **Y**, **W** and **H**. Pick a shape (**Free**, **1:1**, **9:16** or **16:9**) to lock the box to it. **Reset** goes back to the full frame. Click **Done** (or press `C` or `Esc`) when you're finished; the box stays outlined on the video. The crop applies to every clip of this video.
5. Click **Export clips** (bottom of the Properties panel). Each segment is saved as its own MP4, numbered in time order: `interview.mp4` → `interview_1.mp4`, `interview_2.mp4`, … You can cancel at any time. Clips that are already finished are kept. Exported clips get a ✓ on the timeline and turn green in the segment list.
6. Click **Open in Labeler** to label the new clips. The labeler setup opens with the output folder filled in.

Shortcuts: `Space` play/pause · `←` / `→` 5 s back/forward (`Shift`: 1 s) · `,` / `.` one frame · `↑` / `↓` previous/next segment edge or marker · `I` / `O` mark in/out · `M` marker · `C` crop · `Delete` delete the selected segment or marker · `Ctrl+Z` undo · `Ctrl`+mouse wheel or `+` / `-` zoom · `0` show the whole video · `Esc` cancel a started segment or finish cropping.

Notes:

- **Cuts are frame-accurate.** Clips are re-encoded to H.264/AAC MP4, so they always play in the labeler. This takes time: about 1–3 minutes of work for every 10 minutes of clips, depending on the PC.
- **Your segments and crop are saved as you work**, in `_splits.json` in the output folder. Open the same folders again to continue. The file also records which part of which source video every exported clip came from, including the crop box (in pixels of the source video).
- **Changing the crop after exporting** doesn't change clips that are already saved. Their names turn grey in the segment list again and lose their ✓ on the timeline; export again to update them.
- The crop's width and height are rounded to even pixels, because H.264 video needs an even size. With **9:16** or **16:9**, the clip's size can therefore be a pixel or two off the exact ratio (**1:1** is always exactly square).
- **Exporting a video again replaces its old clips.** The app asks first. Old clips that aren't in the new segment list are deleted, so the clips always match the segments.
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
