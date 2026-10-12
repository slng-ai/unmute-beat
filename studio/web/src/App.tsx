import { AgentsPage } from './pages/Agents';
import { Boot } from './pages/Boot';
import { GoBack } from './pages/GoBack';
import { MetricsPage } from './pages/Metrics';
import { Runs } from './pages/Runs';
import { RunView } from './pages/RunView';
import { useRoute } from './router';

export function App() {
  const route = useRoute();
  switch (route.page) {
    case 'boot':
      return <Boot />;
    case 'agents':
      return <AgentsPage key={route.agent ?? ''} route={route} />;
    case 'runs':
      return <Runs route={route} />;
    case 'run':
      return <RunView key={route.run} route={route} />;
    case 'back':
      return <GoBack route={route} />;
    case 'metrics':
      return <MetricsPage route={route} />;
  }
}
