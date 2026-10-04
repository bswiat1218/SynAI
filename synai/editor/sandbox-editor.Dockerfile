FROM python:3.12-slim-trixie

RUN apt-get update \
    && apt-get install -y --no-install-recommends neovim git ca-certificates \
    && apt-get clean

ARG MINI_COMMIT=94cae4660a8b2d95dbbd56e1fbc6fcfa2716d152
RUN git init /opt/synai/mini.nvim \
    && git -C /opt/synai/mini.nvim remote add origin https://github.com/nvim-mini/mini.nvim.git \
    && git -C /opt/synai/mini.nvim fetch --depth 1 origin "${MINI_COMMIT}" \
    && git -C /opt/synai/mini.nvim checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/synai/mini.nvim rev-parse HEAD)" = "${MINI_COMMIT}" \
    && nvim --headless -u NONE \
       "+lua assert(vim.fn.has('nvim-0.10') == 1); vim.opt.runtimepath:prepend('/opt/synai/mini.nvim'); require('mini.ai').setup()" \
       +qa

WORKDIR /workspace
