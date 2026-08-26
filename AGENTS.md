# Axonic project instructions

## Project map

- Backend repository: `D:\Proyects\Axonic` (Django, Django REST Framework, and Channels).
- Mobile repository: `D:\Proyects\Axonic-app` (Expo/React Native development build).
- Android application ID: `com.axonic`.
- Production API: `https://chat.qbared.com`.
- The single authenticated realtime connection is called **Axion** and uses `wss://chat.qbared.com/ws/notifications/`.
- The backend is deployed on Railway. The user normally deploys backend changes and will say when deployment is complete.
- Do not commit, push, deploy, reinstall apps, wipe emulator data, or run a production build unless the user explicitly asks.
- Never store account passwords, access tokens, Railway credentials, or other secrets in repository instructions or scripts.

## Saved command: "start the environment"

When the user says **"start the environment"**, treat it as authorization to prepare the normal Axonic development setup below. Do not ask them to repeat these details. Check what is already running first and start only missing pieces.

1. Start or verify these two Android Virtual Devices:
   - `Medium_Phone_API_36.1`
   - `Medium_Phone_API_36_1_B`
   Use `%ANDROID_HOME%\emulator\emulator.exe` with `-no-snapshot-save`. They normally appear as `emulator-5554` and `emulator-5556`, but always discover the actual serials with `adb devices` instead of assuming the ports.
2. Wait until both emulators have fully booted and ADB reports each one as `device`. Do not reset their data; their Axonic test users and local databases must remain intact.
3. In `D:\Proyects\Axonic-app`, start Expo with `npx expo start --dev-client` in a Codex-managed terminal session. Keep that session available so future requests such as **"check Expo"** or **"monitor the console"** mean reading that same live output. If Metro is already healthy, reuse it instead of starting a duplicate.
4. Verify `com.axonic` is installed on both emulators. Bring `com.axonic/.MainActivity` to the foreground when practical. Do not reinstall merely to launch it; report if either emulator requires login or lacks the development build.
5. Use the user's existing signed-in Chrome session to locate and reuse the Railway project/deployment/logs tab. Do not open duplicate Railway tabs when one already exists. **"check Railway"** means inspect the current deployment and live logs in that tab. Do not trigger a deployment or change Railway settings unless explicitly asked.
6. Check `https://chat.qbared.com/health/` and confirm Metro is reachable by both apps. Do not start a local Django server unless the user specifically requests local-backend testing.
7. Finish with a short readiness report covering both emulator serials, Expo/Metro, the Railway tab, backend health, and any login/build blocker.

## Development-session conventions

- Prefer keeping Expo in a terminal session started by Codex so its logs remain directly monitorable.
- Reuse existing processes and browser tabs; avoid duplicate Metro servers, emulators, or Railway sessions.
- For debugging, correlate Expo logs, ADB/logcat output from both devices, and Railway logs before changing connection code.
- Inspect the current emulator screen before sending ADB input. Do not send test messages to real users unless the user authorizes that specific test.
- Emulator-to-emulator message and call testing is allowed when the user asks to run tests, but preserve the existing accounts and local data.
- A native dependency, Android manifest, config-plugin, or `app.json` change may require rebuilding/reinstalling the development client. Explain that need instead of doing it automatically.

## English-learning feedback

- The user is a native Spanish speaker who wants to improve their English. In every response, answer the actual request first and then add a short section titled **English feedback**.
- Evaluate only the user's own conversational English, not pasted code, logs, error messages, quotations, filenames, or generated text.
- When improvement is useful, include a natural American English version under **More natural:** followed by one or two concise explanations of the most valuable grammar, vocabulary, punctuation, or phrasing changes.
- When the message already sounds natural, say so briefly. A polished alternative may still be offered when it teaches a useful nuance.
- Preserve the user's intended meaning and tone. Do not make their language unnecessarily formal.
- Keep this coaching encouraging and compact so it does not distract from the Axonic work.
