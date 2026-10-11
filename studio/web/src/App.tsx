import { useEffect } from 'react';
import { LeaveDialog } from './components/LeaveDialog';
import { AgentsPage } from './pages/Agents';
import { Boot } from './pages/Boot';
import { GoBack } from './pages/GoBack';
import { MetricsPage } from './pages/Metrics';
import { Runs } from './pages/Runs';
import { RunView } from './pages/RunView';
import { title, useRoute, type Route } from './router';

/** The page for a route. */
function Page({ route }: { route: Route }) {
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

/** The page the address names, its title on the window and the Dock, and the question asked before an unsaved edit is lost. */
export function App() {
  const { route, leaving, stay, leave } = useRoute();
  useEffect(() => {
    document.title = title(route);
  }, [route]);
  return (
    <>
      <Page route={route} />
      <LeaveDialog to={leaving} onStay={stay} onLeave={leave} />
    </>
  );
}
