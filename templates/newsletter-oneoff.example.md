---
subject: "A note from Unseen Japan"
preview: "Some quick thoughts plus what's coming next."
title: "One-off: example draft"
signoff: true
---

# Hello loyal UJ reader,

This is a one-off newsletter written entirely in markdown. Drop your subject
and preview text in the frontmatter above, write the body here, and run:

    python newsletter-oneoff.py path/to/this-file.md

## What you can do

You get the usual markdown things: **bold**, *italic*,
[links](https://unseen-japan.com), inline `code`, and:

- bulleted lists
- with multiple items
- and even nested ones

> Block quotes work too, and render with the UJ-style left-rule callout.

![A descriptive alt text](https://unseen-japan.com/wp-content/uploads/2024/01/some-image.jpg)

## When you're ready

Use `--dump-html` to preview locally, then run without it to push the draft
to Mailchimp. Set a segment in the Mailchimp UI before sending.
