# Leti Cook for Chromium

Add channels to your Leti Cook installation while browsing YouTube. The
turquoise **+** appears beside **Subscribe** on supported channel and video
pages, and in the right-hand action column on Shorts. This is an early version;
YouTube layout changes may require extension updates.

## Install and connect

1. Keep the extension folder on your computer. Extract it first if you downloaded
   a ZIP, and use files from a trusted project source.
2. Open `chrome://extensions`, `edge://extensions` or your browser's extensions
   page. Enable **Developer mode** and choose **Load unpacked**.
3. Select the folder containing `manifest.json`.
4. Open the extension's **Options** or click its toolbar icon. Enter the exact
   local app address, such as `http://127.0.0.1:8002`.
5. Select **Save and connect**, grant access, and sign in to Leti Cook if needed.
6. Keep the app tab open and reload YouTube.

The extension supports HTTP on `127.0.0.1` and `localhost`. Use your installation's
actual address and port. For a remote server, keep the SSH tunnel open too.
No separate YouTube API key is needed.

## Using the button

Click **+** to add the current video's author or the channel you are viewing.
The app opens if needed. **Added** means the request was accepted; catalogue
loading continues in Leti Cook. Complete any large-channel confirmation there.

| Status | Meaning |
| --- | --- |
| **Already added** / check mark | The app confirms the channel is already active. |
| **Restored** | The channel was restored from Deleted channels. |
| **?** / status unavailable | Tracking could not be confirmed; this does not mean the channel is absent. |
| Sign-in or connection notice | Open the app, sign in or reconnect, then return to YouTube. |

An open, connected app tab allows passive tracking checks. These checks do not
add or restore channels and use no YouTube API quota. Explicit additions use the
app's normal collection and quota rules.

If an add request has an uncertain result, check the app before clicking again.
If login was required, sign in and then retry from YouTube.

## Missing or misplaced button

Reload YouTube after loading or updating the extension. If the layout does not
identify one author clearly, open that author's channel page. Some channel
headers depend on English, Russian or Ukrainian subscription labels.

On crowded video pages, the button can move to the lower-right corner. On Shorts
it follows the active video's right-hand controls; its tooltip opens to the
right. Reduced-motion settings disable animation. Other extensions and YouTube
layout changes can affect compatibility.

For connection errors, check the app tab, login, configured address and SSH
tunnel. Include any displayed `LC-...` code when reporting a persistent error.

## Update or remove

Replace the extension files with the new version, click **Reload** on the
browser's extensions page, then reload YouTube. Backend updates do not update
this folder automatically. Removing the extension does not remove your channels.

## Privacy

The extension remembers your configured local address between browser sessions.
Temporary channel-identity hints are reused for up to 30 minutes and cleared
when the browser session ends. It does not store passwords, session cookies or
API keys, collect watch history, or send telemetry. Its actions use your existing
Leti Cook session. Install it only from trusted files.
