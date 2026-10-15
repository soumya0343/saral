---
title: Saral
emoji: 🪷
colorFrom: indigo
colorTo: green
sdk: gradio
sdk_version: 6.30.0
python_version: "3.12"
app_file: app.py
pinned: false
short_description: Multilingual (EN/HI/Hinglish) support agent for insurers
---

# Saral — सरल

A supervisor-orchestrated multi-agent system that resolves insurance support queries in English,
Hindi and Hinglish, grounded in each customer's own policy documents. Writes never happen in the
chat: a confirmed change becomes an encrypted, deduplicated request for the customer's
relationship manager.

**Demo data is synthetic.** OTPs appear as a simulated SMS popup (there is no SMS channel). Tap a
demo account on the sign-in screen, for example Meena Kumari and "मेरा क्लेम क्यों अस्वीकार हुआ?".

Source, architecture and evaluation: see the GitHub repository. This Space is assembled from it by
`deploy/hf-space/build.sh`; Postgres and Redis run inside the Space and the database is dumped to
a storage bucket so it survives restarts.
