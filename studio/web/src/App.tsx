import { Boot } from './pages/Boot';
import { Config } from './pages/Config';
import { Live } from './pages/Live';
import { MetricsPage } from './pages/Metrics';
import { Runs } from './pages/Runs';
import { useRoute } from './router';

export function App() {
  const route = useRoute();
  switch (route.page) {
    case 'boot':
      return <Boot />;
    case 'live':
      return <Live route={route} />;
    case 'config':
      return <Config route={route} />;
    case 'runs':
      return <Runs route={route} />;
    case 'metrics':
      return <MetricsPage route={route} />;
  }
}
