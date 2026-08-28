# SOIA Flow

Ditado por voz para Windows com Whisper na nuvem (Groq) — um adendo do
serviço SOIA CRC. Uma barrinha discreta
fica no centro-inferior da tela: segure o atalho global (padrão
`Ctrl+Shift+Espaço`), fale e solte — o texto é transcrito em ~2 s, copiado para
a área de transferência e colado automaticamente onde o cursor estiver.

## Recursos

- Segurar para falar, soltar para transcrever (ou clique na barrinha)
- Colagem automática no campo ativo (a barrinha nunca rouba o foco)
- Balão com as ondas da voz durante a gravação
- Dicionário personalizado (nomes e termos enviados como contexto ao Whisper)
- Corte de silêncio + filtro de alucinações ("obrigado", "ok"…)
- Token do Groq guardado no Cofre de Credenciais do Windows
- Ícone na bandeja (clique abre as Configurações), opção de iniciar com o Windows
- Leve: sem PyTorch, sem Electron — a transcrição roda na nuvem do Groq

## Instalação

Requisitos: Windows 10/11, Python 3.11+ e um token gratuito do Groq
([console.groq.com/keys](https://console.groq.com/keys)).

```bash
bash instalar.sh
```

Depois abra o `Transcritor.bat`. Na primeira execução, cole o token nas
Configurações e clique em "Testar conexão".

## Arquivos

| Arquivo | Função |
|---|---|
| `transcritor.py` | Aplicativo completo (arquivo único) |
| `instalar.sh` | Cria o venv e instala as dependências |
| `Transcritor.bat` | Abre o app sem janela de console |
| `requirements.txt` | Dependências Python |

Configuração e log ficam em `%APPDATA%\TranscritorDesktop\`.
