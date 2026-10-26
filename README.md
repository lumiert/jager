# Jager

Assistente pessoal local em português. Esta primeira versão inclui uma interface PySide6 em tema escuro, chat com Ollama, histórico SQLite, notas, tarefas e abertura de uma lista restrita de aplicativos e pastas.

## Requisitos

- Windows 10/11 e Python 3.10 ou mais recente.
- Ollama instalado e em execução para conversar com o modelo.
- Um modelo compatível instalado no Ollama. A configuração inicial usa `qwen3:1.7b`.

O núcleo de persistência e os testes também funcionam sem Ollama e sem a interface gráfica.

## Instalação e execução no Windows

No PowerShell, dentro da pasta do projeto:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
ollama pull qwen3:1.7b
python main.py
```

Se o comando `ollama` não for encontrado, instale o Ollama para Windows em <https://ollama.com/download/windows>, abra-o e repita `ollama pull qwen3:1.7b`. O aplicativo verifica a conexão e os modelos instalados sem bloquear a janela; use **Atualizar** para tentar novamente.

Copie `.env.example` para `.env` para configurar `JAGER_OLLAMA_URL` e `JAGER_OLLAMA_MODEL`. O endereço e o modelo também ficam salvos nas configurações locais; o endereço pode ser alterado pela opção **Configurações** na barra lateral.

O banco de dados é criado automaticamente em `%LOCALAPPDATA%\Jager\jager.sqlite3`. Conversas, notas, tarefas e preferências não são enviados para fora do computador pelo aplicativo. O conteúdo do chat é enviado ao servidor Ollama configurado para produzir respostas.

## Comandos disponíveis

- `Anote que preciso comprar filamento PETG`
- `Minhas notas`
- `Crie uma tarefa revisar o projeto`
- `Minhas tarefas`
- `Conclua a tarefa 1`
- `Abra o navegador`, `Abra o VS Code`, `Abra o Mixxx` ou `Abra o Explorador`
- `Abra a pasta Downloads`, `Documentos`, `Área de trabalho` ou `Músicas`
- `Pesquise na internet por modelos locais leves`

Notas e tarefas são tratadas localmente. Abertura de aplicativos usa apenas nomes permitidos e não executa comandos fornecidos pelo modelo ou pelo usuário. Os caminhos de pastas disponíveis correspondem a pastas conhecidas dentro do perfil do usuário.

A pesquisa abre os termos no navegador padrão; ela não lê resultados nem os envia ao modelo.

## Testes

Com Python instalado, execute os testes sem instalar dependências adicionais:

```powershell
python -m unittest discover -s tests -v
```

## Fora do escopo desta versão

Telegram, áudio/STT/TTS, lembretes e agendador, provedores online, CRUD de exclusão, automação de interface e configuração de caminhos personalizados ainda não foram implementados. O status dessas integrações aparece como fase futura; elas não são simuladas como conectadas.
