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

Quando um caminho estiver configurado, o assistente pode incluir até cinco notas com termos correspondentes como contexto de uma pergunta. O conteúdo das notas é tratado como dado não confiável, não como instruções.

## Streaming e áudio

As respostas são lidas progressivamente do endpoint de streaming `/api/chat` do Ollama e atualizadas pela thread principal da interface. **Interromper geração** interrompe a leitura do stream; o texto recebido é mantido quando disponível. A conversa registra uma única resposta final.

Esta versão não possui integração de entrada de áudio ou serviço TTS. Por isso, reprodução de áudio progressiva não está implementada e sua opção aparece desativada; nenhum áudio é simulado.

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

## Testes

Com Python instalado, execute os testes sem instalar dependências adicionais:

```powershell
python -m unittest discover -s tests -v
```

## Limitações

Telegram, entrada de áudio/STT, TTS, lembretes e agendador, provedores online e automação de interface não estão disponíveis. O Vault integra arquivos Markdown diretamente, mas não sincroniza nem inicia o aplicativo Obsidian.
