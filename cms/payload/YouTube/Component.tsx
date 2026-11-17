// Renders the youtube block on the front end. Register it in the RichText converters.
import React from 'react'

export const YouTubeBlock: React.FC<{url: string}> = ({url}) => {
  const id = new URL(url).searchParams.get('v')
  return (
    <div className="relative aspect-video w-full">
      <iframe
        src={`https://www.youtube.com/embed/${id}`}
        title="YouTube video"
        className="absolute inset-0 h-full w-full border-0"
        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
        allowFullScreen
      />
    </div>
  )
}
