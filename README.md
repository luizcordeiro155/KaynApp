# Kayn Bot - refatorado para GitHub

Estrutura reorganizada para tirar a execução do `main.py` e preparar o projeto para manutenção por módulos/cogs.

## Estrutura

```text
main.py                 # somente importa o bot e executa bot.run()
core/bot.py             # adapta a inicialização antiga e expõe o bot
cogs/legacy_runtime.py  # runtime original preservado para manter tudo funcionando
cogs/*.py              # cogs por domínio para migração gradual
services/               # regras de negócio
views/                  # discord.ui.View e componentes
utils/                  # helpers
assets/                 # imagens do bot
```

## Como rodar

1. Copie `.env.example` para `.env` e preencha as variáveis reais.
2. Coloque certificados da Efí em `certs/` se usar Pix.
3. Instale dependências:

```bash
pip install -r requirements.txt
```

4. Execute:

```bash
python main.py
```

## Observação importante

Por segurança para GitHub, este pacote não inclui o `.env`, certificados privados nem banco local.
O código original ficou preservado em `cogs/legacy_runtime.py`, e o `main.py` ficou mínimo.
A partir daqui, você pode migrar comandos aos poucos para os cogs específicos sem quebrar o bot em produção.
