# Cordée frontend

React + TypeScript + Vite + TailwindCSS. Primary UI served at `/` (Flask serves `dist/` as static files).

## Build & typecheck

```bash
cd frontend
npm install
npx tsc -b --noEmit    # TypeScript check
npm run build           # Vite production build → dist/
```

Flask serves `frontend/dist/` for `/`. Restart the Flask server after building:

```bash
sudo systemctl restart superagent
```

## Architecture

- **State**: Zustand store (`src/store.ts`)
- **API client**: `src/api.ts` (wraps all ~103 REST endpoints)
- **Query/cache**: TanStack Query via `src/main.tsx` QueryClientProvider

## Components (20+)

| Component | File | Purpose |
|-----------|------|---------|
| Sidebar | `Sidebar.tsx` | Project list + phase accordions, single-open-at-a-time |
| Header | `Header.tsx` | LIVE badge, project selector, cost pill, panel toggles (Chats/Guide/Memory) |
| Dashboard | `Dashboard.tsx` | Home mode: project cards with stats + costs. Project mode: tabs layout |
| TaskBoard | `TaskBoard.tsx` | Kanban columns + board view with scroll sync |
| TaskCard | `TaskCard.tsx` | Clickable title/description → opens unified `AddTaskModal` in edit mode |
| ExecutionLog | `ExecutionLog.tsx` | Task execution history table with Guide review column |
| ChatPanel | `ChatPanel.tsx` | Slide-in chat panel |
| ChatsPanel | `ChatsPanel.tsx` | Slide-in chats list |
| AIngelPanel | `AIngelPanel.tsx` | Slide-in Guide brief/review/overview |
| MemoryPanel | `MemoryPanel.tsx` | Slide-in memory management |
| AddTaskModal | `AddTaskModal.tsx` | Unified create + edit modal (replaces deprecated ModTaskModal) |
| DependenciesModal | `DependenciesModal.tsx` | Dependency graph + management |
| NewProjectModal | `NewProjectModal.tsx` | Create project with type template |
| SettingsModal | `SettingsModal.tsx` | Per-project settings |
| BudgetModal | `BudgetModal.tsx` | Budget tracking |
| DependencyGraph | `DependencyGraph.tsx` | Visual dependency graph |
| Notifications | `Notifications.tsx` | Notification list |
| ProviderIndicators | `ProviderIndicators.tsx` | Provider status indicators |
| SearchModal | `SearchModal.tsx` | Search across tasks |
| AttachPicker | `AttachPicker.tsx` | Chat attachment picker |
| ScaffoldProgress | `ScaffoldProgress.tsx` | Scaffolding progress (SSE) |

## Design

- **Color scheme**: background `#f5ead8`, accent `#c67139`, base font 14px
- **Project colors**: deterministic hash-based HSL hues
- **Slide-in panels**: Chats, Guide, and Memory panels fixed-right, toggled by header buttons
