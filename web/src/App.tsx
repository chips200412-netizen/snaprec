import { CollectionApp } from "./collection-app";
import { ThemeProvider } from "./theme";

export function App() {
  return <ThemeProvider><CollectionApp /></ThemeProvider>;
}
