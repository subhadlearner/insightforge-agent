import { WatchlistDiffView } from "@/components/watchlist-diff-view";

export default async function WatchlistDetailPage({ params }: { params: Promise<{ itemId: string }> }) {
  const { itemId } = await params;
  return <WatchlistDiffView key={itemId} itemId={decodeURIComponent(itemId)} />;
}
