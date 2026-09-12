import { useMemo } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

export default function Preview({ markdown, currentPath }){
  const html = useMemo(()=>{
    const raw = marked.parse(markdown || '', { gfm:true, breaks:true })
    return DOMPurify.sanitize(raw)
  }, [markdown])

  return (
    <div className="md-preview markdown-body">
      <link rel="stylesheet" href={currentPath ? '' : ''} />
      <style>{`@import url('/custom.css');`}</style>
      <div dangerouslySetInnerHTML={{__html: html}} />
    </div>
  )
}
