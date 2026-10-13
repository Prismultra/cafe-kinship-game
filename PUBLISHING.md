# Publishing a new version

Players get the game through the **Games** tab in the Cafe Kinship app for
Windows. When you publish a version here, the app sees it the next time it
opens and downloads **only the files that changed**.

## Every update

1. Package the game in Unreal (Shipping, Windows). The setup that keeps
   updates small is below.
2. Zip the packaged folder: the one with the game's `.exe` in it. Keep the
   zip under 2 GB (GitHub's limit for one file).
3. Here on GitHub: **Releases → Draft a new release**.
   - **Tag:** the new version, like `v0.2.0` (always higher than the last).
   - **Notes:** each line that starts with `- ` shows up in the app under
     "What's new". Keep them short.
   - Attach the zip, then **Publish release**.
4. That's it. **Actions → Publish game files** runs by itself (about 10–20
   minutes). Its summary says how much players on the old version will
   download. When it finishes, the app has the update.

Want to try a build before players get it? Tick **Set as a pre-release**:
pre-releases are skipped. To send it out later, untick it, or run
**Actions → Publish game files → Run workflow** with its tag.

**Don't delete or edit the `game-files` release.** It holds the files the
app downloads, and the workflow tidies it up by itself. Old version releases
are safe to delete.

## Keeping updates small (do this once)

Unreal packs nearly all of the game into one or two huge files in
`Content/Paks`. If that file changes, players download all of it again, even
when you only changed one texture. Unreal's **patch** option fixes that: it
leaves the big file alone and puts your changes in a small extra file.

In Unreal: **Tools → Project Launcher → Custom Launch Profiles → Add**, then:

- **Project:** Cafe Kinship · **Build:** Shipping
- **Cook:** By the Book, platform **Windows**
- **Package:** Package & store locally · **Deploy:** Do not deploy

Then, under the cook settings' **Release / DLC / Patching**:

- **The first time** (the "base" version everyone downloads in full):
  tick **Create a release version** and call it the version number, like
  `0.2`. Unreal saves what's in it to the `Releases/0.2` folder in your
  project. **Keep that folder** (back it up): every patch is worked out
  against it.
- **Every update after that:** untick *Create a release version*, tick
  **Generate patch**, and set *Release version this is based on* to the base
  (`0.2`).

The zip you upload must always be the **whole game**. If a patch build only
gives you the new patch file (it ends in `_P.pak`, or `_P.utoc` and
`_P.ucas`), copy it into `Content/Paks` of your full game folder first, then
zip that folder.

After a lot of patches, make a new base (tick *Create a release version*
again). Players download the full game once more, then patches start small
again.

The patch option is optional: without it the app still works, it just
downloads the big file whenever it changed.
