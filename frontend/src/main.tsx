import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import Root from './router'
import './global.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
