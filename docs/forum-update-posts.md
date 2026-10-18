# BBCode update posts and screenshot galleries

Prepare the BBCode update draft and its dated screenshot gallery together.
Canonical post drafts live in `repo-releases/<repository>/`, named
`<repository>-update-post.bbcode.txt`.

## Dated screenshot folder

For every update post that includes screenshots, create
`docs/update/YYYY-MM-DD/` in the extension repository. Use the date of the
update being prepared, rather than moving images from earlier updates.

Include only the two or three screenshots needed to show the recent changes.
Keep full-resolution originals; use clear, descriptive filenames. Refreshing
the complete documentation gallery is a separate task.

Every dated folder must contain its own `README.md`, prepared in the same
change as the images and BBCode draft. The README must have:

- A project-specific heading and human-readable update date (`MM/DD/YYYY`).
- A short caption explaining the recent change shown in each screenshot.
- A thumbnail for every image, linked to the full-size file with a relative link.
- An HTML `<img>` with useful `alt` text and explicit `width` and `height`
  attributes preserving the source aspect ratio. Use a display width of 640
  pixels unless the project has an established alternative; do not resize the
  original files to achieve that display size.

For example:

```markdown
# Project update screenshots — 10/18/2026

A short caption describing the change shown here.

[<img src="changed-feature.png" width="640" height="450" alt="A short description of the changed feature." />](changed-feature.png)
```

Calculate the height from the actual image dimensions; the example's height
is not a fixed value for other images.

## Link the BBCode post to the dated gallery

Use stock BBCode. Put the update version first, followed by the download or
release link and source commit, then concise sections explaining the changes.
Use the images in the dated folder for the post's screenshot URLs and include
a link to that folder's `README.md`. A link to the full screenshot gallery may
also be included, but does not replace the dated gallery link.

```bbcode
[b]Screenshots[/b]
[img]https://raw.githubusercontent.com/OWNER/REPOSITORY/main/docs/update/YYYY-MM-DD/changed-feature.png[/img]

[url=https://github.com/OWNER/REPOSITORY/blob/main/docs/update/YYYY-MM-DD/README.md]Screenshots for this update[/url]
```

Before finishing preparation, verify that every post image exists in the
dated folder, every image appears in its README, and every thumbnail link,
caption, dimension and BBCode URL matches. Include the README when exporting
the dated screenshot set for review.

Publication remains a separate approval step: preparing the files does not
post the announcement, merge source changes, or refresh a rolling release.
