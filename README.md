# Jager

Assistente pessoal local em português, com interface PySide6 em tema escuro, chat com streaming real do Ollama, histórico SQLite, Markdown sanitizado, gerenciamento de conversas e integração opcional com um Vault do Obsidian.

## Requisitos

- Windows 10/11 e Python 3.10 ou mais recente.
- Ollama instalado e em execução para conversar com o modelo e criar títulos automáticos.
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

Copie `.env.example` para `.env` para configurar `JAGER_OLLAMA_URL` e `JAGER_OLLAMA_MODEL`. O endereço e o modelo também ficam salvos nas configurações locais. O seletor lista somente modelos que o servidor Ollama informar como instalados; use **Atualizar modelos** para consultar novamente.

O banco de dados é criado automaticamente em `%LOCALAPPDATA%\Jager\jager.sqlite3`. Conversas, notas locais, tarefas e preferências não são enviados para fora do computador pelo aplicativo. O conteúdo do chat é enviado ao servidor Ollama configurado para produzir respostas.

## Conversas e Markdown

Conversas novas começam como **Nova conversa**. O Jager pede ao Ollama um título curto em uma chamada separada; se isso falhar, usa o início da primeira mensagem. Títulos renomeados manualmente não são substituídos. O menu de contexto do histórico permite renomear e excluir conversas, sem apagar notas ou arquivos do Vault.

Mensagens aceitam títulos, listas, tabelas, citações, links, código e demais elementos Markdown. HTML arbitrário das respostas não é renderizado e links só são encaminhados ao navegador padrão para esquemas HTTP(S) e mailto. Blocos de código têm uma ação de cópia.

## Vault do Obsidian

Abra **Configurações**, selecione explicitamente a pasta do Vault e depois use **Vault** na barra lateral. O Jager lista arquivos `.md` sem percorrer pastas ocultas nem seguir links simbólicos; permite pesquisar, ler, criar e editar notas. Frontmatter, links `[[wikilinks]]` e o conteúdo Markdown são preservados. Antes de salvar uma nota existente, o aplicativo compara seu conteúdo com a versão carregada e recusa a gravação se detectar uma alteração externa. O Jager não move nem exclui arquivos do Vault.

Quando um caminho estiver configurado, o assistente pode pesquisar notas relevantes no Vault. O conteúdo retornado pela ferramenta é tratado como dado não confiável, nunca como instruções.

## Streaming, áudio e dispositivos

As respostas são lidas progressivamente do endpoint de streaming `/api/chat` do Ollama e atualizadas pela thread principal da interface. **Interromper geração** interrompe a leitura do stream; o texto recebido é mantido quando disponível. A conversa registra uma única resposta final.

O botão de microfone implementa PTT: mantenha-o pressionado para gravar e solte para transcrever. A captura usa `sounddevice`/PortAudio em uma thread e o STT local usa `faster-whisper` no modelo `tiny`, em CPU/int8, com idioma português. O modelo Whisper é baixado pelo CTranslate2 no primeiro uso; esse download inicial requer acesso à internet. Áudio temporário fica em arquivo WAV no diretório temporário do sistema e é removido após o reconhecimento. Escape, perda de foco da janela, minimização e fechamento cancelam a gravação; há também um limite de 60 segundos. É necessário conectar um microfone e conceder permissão ao aplicativo.

Instalação: `pip install -r requirements.txt`. No Windows, `sounddevice` usa PortAudio e `faster-whisper` usa CTranslate2; a inferência configurada é em CPU, sem exigir CUDA. Os pacotes não garantem que um dispositivo de gravação esteja conectado ou autorizado.

Em **Configurações**, a preferência do dispositivo de saída é persistida e a lista vem dos dispositivos Qt/Windows; se o dispositivo salvo desaparecer, o padrão do sistema é usado temporariamente, sem mudar o padrão global. Ative **Responder por áudio** para usar síntese local `pyttsx3`/SAPI5 no Windows; a reprodução passa por `QAudioOutput` no dispositivo escolhido. A fala progressiva pode ser habilitada separadamente e sintetiza frases conforme chegam. O sistema prefere uma voz portuguesa instalada e usa a voz padrão do Windows se nenhuma estiver disponível. Nenhum áudio de saída é simulado.

## Comandos disponíveis

- `Anote que preciso comprar filamento PETG` (cria um `.md` no Vault configurado)
- `Minhas notas`
- `Crie uma tarefa revisar o projeto`
- `Minhas tarefas`
- `Conclua a tarefa 1`
- `Abra o navegador`, `Abra o VS Code`, `Abra o Mixxx` ou `Abra o Explorador`
- `Abra a pasta Downloads`, `Documentos`, `Área de trabalho` ou `Músicas`
- `Pesquise na internet por modelos locais leves`

Tarefas são tratadas localmente; novas notas são criadas no Vault configurado. Registros antigos de notas no banco de dados são preservados. Abertura de aplicativos usa apenas nomes permitidos e não executa comandos fornecidos pelo modelo ou pelo usuário. Os caminhos de pastas disponíveis correspondem a pastas conhecidas dentro do perfil do usuário.

A pesquisa abre os termos no navegador padrão; ela não lê resultados nem os envia ao modelo.

O Ollama recebe a lista JSON Schema das ferramentas realmente registradas. Chamadas estruturadas são validadas e executadas pela aplicação; modelos sem suporte a ferramentas usam apenas um envelope JSON estrito, nunca comandos de terminal. Pastas de leitura são limitadas a Downloads, Documentos, Área de trabalho e Vault; criações no Vault e ações externas pedem confirmação. A pesquisa web com resultados não está disponível. O ciclo de ferramentas tem limite de quatro iterações (ajustável pela preferência local `tool_iteration_limit`, limitado a 1–8).

## Telegram

As configurações incluem campo mascarado para token. O segredo é armazenado via `keyring` no cofre de credenciais do sistema (Windows Credential Manager no Windows); não há fallback para SQLite ou arquivo em texto puro. O botão de olho revela o segredo somente enquanto pressionado e o esconde ao soltar ou ao perder o foco. A integração e o teste de conexão ainda não estão implementados; salvar o token não faz requisições ao Telegram.

## Testes

Com as dependências do projeto instaladas, execute os testes:

```powershell
python -m unittest discover -s tests -v
```

## Limitações

Conexão/teste de Telegram, lembretes e agendador, pesquisa web que leia resultados e automação de interface não estão disponíveis. O STT depende de microfone/permissões e baixa o modelo Whisper no primeiro uso. O TTS depende de uma voz instalada e de um backend Qt Multimedia disponível. O Vault integra arquivos Markdown diretamente, mas não sincroniza nem inicia o aplicativo Obsidian.
