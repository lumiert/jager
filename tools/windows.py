from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote_plus
import webbrowser


APPLICATIONS = {
    "explorador": ("explorer", "nautilus", "thunar"),
    "navegador": ("", "", ""),
    "vs code": ("code", "code", "code"),
    "mixxx": ("mixxx", "mixxx", "mixxx"),
}

FOLDERS = {
    "downloads": "Downloads",
    "documentos": "Documents",
    "área de trabalho": "Desktop",
    "desktop": "Desktop",
    "músicas": "Music",
}


def open_application(name: str) -> None:
    normalized = name.strip().lower()
    if normalized == "navegador":
        if not webbrowser.open("https://www.google.com"):
            raise RuntimeError("Não foi possível abrir o navegador padrão.")
        return
    commands = APPLICATIONS.get(normalized)
    if commands is None:
        raise ValueError("Esse aplicativo não está na lista de aplicativos autorizados.")
    index = 0 if os.name == "nt" else 1 if sys.platform == "darwin" else 2
    command = commands[index]
    if not command:
        raise RuntimeError("Esse aplicativo não está disponível nesta plataforma.")
    executable = shutil.which(command)
    if executable is None:
        raise FileNotFoundError(f"Não encontrei o aplicativo autorizado “{name}”.")
    subprocess.Popen([executable], shell=False)


def open_folder(name: str) -> None:
    normalized = name.strip().lower()
    folder = FOLDERS.get(normalized)
    if folder is None:
        raise ValueError("Essa pasta não está na lista de pastas autorizadas.")
    path = Path.home() / folder
    if not path.is_dir():
        raise FileNotFoundError(f"A pasta “{folder}” não existe neste computador.")
    if os.name == "nt":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)], shell=False)
    else:
        opener = shutil.which("xdg-open")
        if opener is None:
            raise RuntimeError("O utilitário para abrir pastas não está disponível.")
        subprocess.Popen([opener, str(path)], shell=False)


def search_web(query: str) -> None:
    query = query.strip()
    if not query:
        raise ValueError("Informe o que deseja pesquisar.")
    url = f"https://www.google.com/search?q={quote_plus(query)}"
    if not webbrowser.open(url):
        raise RuntimeError("Não foi possível abrir a pesquisa no navegador.")
