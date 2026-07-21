from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["Voice"])


VOICE_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta
        name="viewport"
        content="width=device-width, initial-scale=1"
    >
    <title>FRIDAY Voice</title>

    <style>
        :root {
            color-scheme: dark;
            font-family:
                Inter,
                ui-sans-serif,
                system-ui,
                -apple-system,
                BlinkMacSystemFont,
                "Segoe UI",
                sans-serif;
        }

        * {
            box-sizing: border-box;
        }

        body {
            min-height: 100vh;
            margin: 0;
            display: grid;
            place-items: center;
            background:
                radial-gradient(
                    circle at top,
                    #172554 0,
                    #07101f 42%,
                    #020617 100%
                );
            color: #e2e8f0;
        }

        main {
            width: min(760px, calc(100% - 32px));
            padding: 32px;
            border: 1px solid rgba(148, 163, 184, 0.22);
            border-radius: 28px;
            background: rgba(15, 23, 42, 0.82);
            box-shadow:
                0 30px 90px rgba(0, 0, 0, 0.5);
            backdrop-filter: blur(18px);
        }

        header {
            display: flex;
            justify-content: space-between;
            gap: 20px;
            align-items: center;
            margin-bottom: 30px;
        }

        h1 {
            margin: 0;
            font-size: clamp(34px, 7vw, 62px);
            letter-spacing: 0.12em;
        }

        .online {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 14px;
            color: #94a3b8;
        }

        .online-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #22c55e;
            box-shadow: 0 0 16px #22c55e;
        }

        .panel {
            padding: 20px;
            margin-top: 16px;
            border: 1px solid rgba(148, 163, 184, 0.16);
            border-radius: 18px;
            background: rgba(2, 6, 23, 0.52);
        }

        .label {
            margin-bottom: 8px;
            color: #94a3b8;
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.15em;
            text-transform: uppercase;
        }

        #status {
            min-height: 28px;
            font-size: 18px;
        }

        textarea {
            width: 100%;
            min-height: 120px;
            resize: vertical;
            border: 0;
            outline: none;
            background: transparent;
            color: #f8fafc;
            font: inherit;
            font-size: 18px;
            line-height: 1.55;
        }

        #reply {
            min-height: 90px;
            white-space: pre-wrap;
            font-size: 19px;
            line-height: 1.6;
        }

        .controls {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 14px;
            margin-top: 20px;
        }

        button {
            min-height: 58px;
            border: 0;
            border-radius: 16px;
            padding: 14px 18px;
            cursor: pointer;
            font: inherit;
            font-weight: 750;
            transition:
                transform 120ms ease,
                opacity 120ms ease,
                box-shadow 120ms ease;
        }

        button:hover:not(:disabled) {
            transform: translateY(-2px);
        }

        button:disabled {
            cursor: not-allowed;
            opacity: 0.5;
        }

        #listenButton {
            color: #eff6ff;
            background:
                linear-gradient(
                    135deg,
                    #2563eb,
                    #7c3aed
                );
            box-shadow:
                0 12px 34px rgba(59, 130, 246, 0.28);
        }

        #listenButton.listening {
            background:
                linear-gradient(
                    135deg,
                    #dc2626,
                    #db2777
                );
            box-shadow:
                0 12px 34px rgba(220, 38, 38, 0.32);
        }

        #sendButton {
            color: #082f49;
            background: #7dd3fc;
        }

        .secondary-controls {
            display: flex;
            gap: 12px;
            margin-top: 14px;
        }

        .secondary-controls button {
            min-height: 42px;
            padding: 9px 14px;
            color: #cbd5e1;
            border: 1px solid rgba(148, 163, 184, 0.22);
            background: rgba(30, 41, 59, 0.72);
        }

        .hint {
            margin: 18px 0 0;
            color: #64748b;
            font-size: 13px;
            line-height: 1.5;
        }

        @media (max-width: 600px) {
            main {
                padding: 22px;
                border-radius: 20px;
            }

            header {
                align-items: flex-start;
                flex-direction: column;
            }

            .controls {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>

<body>
<main>
    <header>
        <h1>FRIDAY</h1>

        <div class="online">
            <span class="online-dot"></span>
            <span>Assistant online</span>
        </div>
    </header>

    <section class="panel">
        <div class="label">Status</div>
        <div id="status">Ready.</div>
    </section>

    <section class="panel">
        <div class="label">You</div>

        <textarea
            id="transcript"
            placeholder="Press Listen and speak, or type a request here."
            autocomplete="off"
        ></textarea>
    </section>

    <div class="controls">
        <button id="listenButton" type="button">
            🎙 Listen
        </button>

        <button id="sendButton" type="button">
            Send to FRIDAY
        </button>
    </div>

    <div class="secondary-controls">
        <button id="stopSpeechButton" type="button">
            Stop speaking
        </button>

        <button id="clearButton" type="button">
            Clear
        </button>
    </div>

    <section class="panel">
        <div class="label">FRIDAY</div>
        <div id="reply">Waiting for your request.</div>
    </section>

    <p class="hint">
        Microphone recognition uses your browser's speech-recognition
        capability. Chrome or Safari usually works best on localhost.
        You can always type and press Send.
    </p>
</main>

<script>
(() => {
    "use strict";

    const transcript = document.getElementById("transcript");
    const reply = document.getElementById("reply");
    const status = document.getElementById("status");

    const listenButton =
        document.getElementById("listenButton");

    const sendButton =
        document.getElementById("sendButton");

    const clearButton =
        document.getElementById("clearButton");

    const stopSpeechButton =
        document.getElementById("stopSpeechButton");

    const Recognition =
        window.SpeechRecognition
        || window.webkitSpeechRecognition;

    let recognition = null;
    let listening = false;
    let finalTranscript = "";

    function setStatus(message) {
        status.textContent = message;
    }

    function setBusy(busy) {
        sendButton.disabled = busy;
        listenButton.disabled = busy;
    }

    function newRequestId() {
        if (
            window.crypto
            && typeof window.crypto.randomUUID === "function"
        ) {
            return `voice-${window.crypto.randomUUID()}`;
        }

        return (
            `voice-${Date.now()}-`
            + Math.random().toString(16).slice(2)
        );
    }

    function speak(text) {
        if (
            !text
            || !("speechSynthesis" in window)
        ) {
            return;
        }

        window.speechSynthesis.cancel();

        const utterance =
            new SpeechSynthesisUtterance(text);

        utterance.rate = 1.02;
        utterance.pitch = 0.96;
        utterance.volume = 1;

        const voices =
            window.speechSynthesis.getVoices();

        const preferred =
            voices.find(
                (voice) =>
                    /samantha|daniel|alex|google us english/i
                        .test(voice.name)
            )
            || voices.find(
                (voice) =>
                    voice.lang
                    && voice.lang.startsWith("en")
            );

        if (preferred) {
            utterance.voice = preferred;
        }

        utterance.onstart = () => {
            setStatus("FRIDAY is speaking.");
        };

        utterance.onend = () => {
            setStatus("Ready.");
        };

        utterance.onerror = () => {
            setStatus(
                "Speech output could not be played."
            );
        };

        window.speechSynthesis.speak(utterance);
    }

    async function sendRequest() {
        const text = transcript.value.trim();

        if (!text) {
            setStatus("Say or type something first.");
            transcript.focus();
            return;
        }

        setBusy(true);
        setStatus("FRIDAY is thinking.");
        reply.textContent = "Working…";

        try {
            const response = await fetch(
                "/v1/assist",
                {
                    method: "POST",
                    headers: {
                        "Content-Type":
                            "application/json",
                        "Accept":
                            "application/json",
                    },
                    body: JSON.stringify({
                        text,
                        request_id: newRequestId(),
                        metadata: {
                            channel:
                                "browser-voice",
                        },
                    }),
                },
            );

            const payload = await response.json();

            if (!response.ok) {
                const message =
                    payload.message
                    || `Request failed with HTTP ${response.status}.`;

                throw new Error(message);
            }

            const answer =
                payload.response
                || "Done.";

            reply.textContent = answer;
            setStatus("Completed.");
            speak(answer);
        } catch (error) {
            const message =
                error instanceof Error
                    ? error.message
                    : String(error);

            reply.textContent =
                `I couldn't complete that request: ${message}`;

            setStatus("Request failed.");
        } finally {
            setBusy(false);
        }
    }

    function stopListening() {
        if (!recognition || !listening) {
            return;
        }

        recognition.stop();
    }

    function startListening() {
        if (!Recognition) {
            setStatus(
                "This browser does not support microphone transcription. "
                + "Type your request instead."
            );
            return;
        }

        if (listening) {
            stopListening();
            return;
        }

        finalTranscript = "";

        recognition = new Recognition();
        recognition.lang = "en-US";
        recognition.continuous = false;
        recognition.interimResults = true;
        recognition.maxAlternatives = 1;

        recognition.onstart = () => {
            listening = true;
            listenButton.classList.add("listening");
            listenButton.textContent = "■ Stop";
            setStatus("Listening…");
        };

        recognition.onresult = (event) => {
            let interimTranscript = "";

            for (
                let index = event.resultIndex;
                index < event.results.length;
                index += 1
            ) {
                const result = event.results[index];
                const text = result[0].transcript;

                if (result.isFinal) {
                    finalTranscript += `${text} `;
                } else {
                    interimTranscript += text;
                }
            }

            transcript.value =
                `${finalTranscript}${interimTranscript}`
                    .trim();
        };

        recognition.onerror = (event) => {
            if (event.error === "not-allowed") {
                setStatus(
                    "Microphone permission was denied."
                );
                return;
            }

            if (event.error === "no-speech") {
                setStatus(
                    "I didn't hear anything. Try again."
                );
                return;
            }

            setStatus(
                `Speech recognition error: ${event.error}`
            );
        };

        recognition.onend = () => {
            listening = false;
            listenButton.classList.remove("listening");
            listenButton.textContent = "🎙 Listen";

            const captured =
                transcript.value.trim();

            if (captured) {
                setStatus("Speech captured.");
            } else {
                setStatus("Ready.");
            }
        };

        recognition.start();
    }

    listenButton.addEventListener(
        "click",
        startListening,
    );

    sendButton.addEventListener(
        "click",
        sendRequest,
    );

    clearButton.addEventListener(
        "click",
        () => {
            transcript.value = "";
            reply.textContent =
                "Waiting for your request.";
            setStatus("Ready.");

            if ("speechSynthesis" in window) {
                window.speechSynthesis.cancel();
            }

            transcript.focus();
        },
    );

    stopSpeechButton.addEventListener(
        "click",
        () => {
            if ("speechSynthesis" in window) {
                window.speechSynthesis.cancel();
            }

            setStatus("Speech stopped.");
        },
    );

    transcript.addEventListener(
        "keydown",
        (event) => {
            if (
                event.key === "Enter"
                && (event.metaKey || event.ctrlKey)
            ) {
                event.preventDefault();
                void sendRequest();
            }
        },
    );

    if (!Recognition) {
        listenButton.disabled = true;
        listenButton.textContent =
            "Microphone unavailable";

        setStatus(
            "Voice transcription is unsupported here. "
            + "Typing still works."
        );
    }
})();
</script>
</body>
</html>
"""


@router.get(
    "/",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def voice_home() -> HTMLResponse:
    return HTMLResponse(VOICE_PAGE)


@router.get(
    "/voice",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def voice_page() -> HTMLResponse:
    return HTMLResponse(VOICE_PAGE)
