const tg = window.Telegram?.WebApp;

if (tg) {
    tg.ready();
    tg.expand();
}

const playlistInput = document.getElementById("playlist-url");
const searchButton = document.getElementById("search-btn");
const results = document.getElementById("results");

const API_URL = "/api/playlist";

function setStatus(message, className = "status") {
    results.innerHTML = "";
    const p = document.createElement("p");
    p.className = className;
    p.textContent = message;
    results.appendChild(p);
}

async function findTracks() {
    const url = playlistInput.value.trim();

    if (!url) {
        setStatus("Вставьте ссылку на плейлист.", "error");
        return;
    }

    searchButton.disabled = true;
    setStatus("Ищем треки...");

    try {
        const response = await fetch(API_URL, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({ url }),
        });

        if (!response.ok) {
            throw new Error(`Ошибка сервера: ${response.status}`);
        }

        const data = await response.json();
        renderResults(data);
    } catch (error) {
        setStatus(error.message || "Не удалось получить треки.", "error");
    } finally {
        searchButton.disabled = false;
    }
}

function renderResults(data) {
    results.innerHTML = "";

    const tracks = Array.isArray(data) ? data : data.tracks;

    if (!tracks || tracks.length === 0) {
        setStatus("Треки не найдены.");
        return;
    }

    const list = document.createElement("ul");
    list.className = "track-list";

    for (const track of tracks) {
        const item = document.createElement("li");
        item.className = "track-item";
        item.textContent = track.title
            ? `${track.artist ? track.artist + " — " : ""}${track.title}`
            : String(track);
        list.appendChild(item);
    }

    results.appendChild(list);
}

searchButton.addEventListener("click", findTracks);
