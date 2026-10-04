vim.opt.termguicolors = true
vim.opt.hidden = true
vim.opt.number = true
vim.opt.mouse = 'a'
vim.opt.exrc = false
vim.opt.modeline = false
vim.opt.directory = vim.env.SYNAI_EDITOR_DIR .. '/swap//'
vim.opt.runtimepath:prepend(vim.env.SYNAI_MINI_PATH)

for _, name in ipairs({ 'ai', 'surround', 'comment', 'pairs', 'completion', 'statusline', 'tabline' }) do
  require('mini.' .. name).setup()
end

local apply_theme = dofile(vim.env.SYNAI_EDITOR_DIR .. '/theme.lua')
local theme
SynAI = {}

function SynAI.call(operation, data)
  if operation == 'theme' then
    theme = data
    apply_theme(theme)
  elseif operation == 'open' then
    local buffer = vim.fn.bufadd(data.path)
    vim.fn.bufload(buffer)
    vim.api.nvim_set_current_buf(buffer)
  elseif operation == 'state' then
    local modified = {}
    for _, buffer in ipairs(vim.api.nvim_list_bufs()) do
      if vim.api.nvim_buf_is_valid(buffer) and vim.bo[buffer].modified and vim.bo[buffer].buftype == '' then
        table.insert(modified, { buffer = buffer, name = vim.api.nvim_buf_get_name(buffer) })
      end
    end
    return vim.json.encode({ modified = modified })
  elseif operation == 'save' then
    for _, buffer in ipairs(vim.api.nvim_list_bufs()) do
      if vim.api.nvim_buf_is_valid(buffer) and vim.bo[buffer].modified and vim.bo[buffer].buftype == '' then
        local name = data.paths and data.paths[tostring(buffer)]
        if name then
          vim.api.nvim_buf_set_name(buffer, name)
        end
        if vim.api.nvim_buf_get_name(buffer) == '' then
          error('Unnamed buffer needs a save destination')
        end
        vim.api.nvim_buf_call(buffer, function()
          vim.cmd('write')
        end)
      end
    end
  else
    error('Unknown SynAI editor operation: ' .. operation)
  end
  return vim.json.encode({ ok = true })
end

vim.api.nvim_create_autocmd('ColorScheme', {
  callback = function()
    if theme then apply_theme(theme) end
  end,
})
